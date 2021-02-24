"""
Reprojection tool for ITS_LIVE granules.
"""

import argparse
import logging
import numpy as np
from osgeo import osr
from osgeo import gdal
import xarray as xr

from grid import Grid, Bounds


# Set up logging
logging.basicConfig(
    level = logging.INFO,
    format = '%(asctime)s - %(levelname)s - %(message)s',
    datefmt = '%Y-%m-%d %H:%M:%S'
)


class ItsLiveReproject:
    """
    Class to store input ITS_LIVE granule, and functionality to re-project
    its data into a new target projection.

    The following steps must be taken to re-project ITS_LIVE granule to new
    projection:

    1. Compute bounding box for input granule in original P_in projection ("ij" naming convention)
    2. Re-project P_in bounding box to P_out projection ("xy" naming convention)
    3. Compute grid in P_out projection based on its bounding bbox
    4. Project each cell center in P_out grid to original P_in projection: (x0, y0)
    5. Add unit length (240m) to x0 of (x0, y0) and project to P_out: (x1, y1)
    6. Add unit length (240m) to y0 of (x0, y0) and project to P_out: (x2, y2)
    7. In Geogrid code, set normal = (0, 0, 1)
    8. Compute transformation matrix using Geogrid equations
    9. Re-project v* values: gdal.warp(original_granule, P_out_grid) --> P_out_v
       Apply tranformation matrix to P_out_v per cell to get "true" v value
    """
    NODATA_VALUE = -32767

    # Number of seconds in one day: any period would do as long as it's
    # the same time period used to convert v(elocity) to d(istance), and
    # then use the same value to compute transformation matrix
    TIME_DELTA = 24.0 * 3600.0

    def __init__(self, data, output_projection: int):
        """
        Initialize object.
        """
        self.logger = logging.getLogger("ItsLiveReproject")

        self.ds = data
        self.input_file = None
        if isinstance(data, str):
            # Filename for the dataset is provided, read it in
            self.input_file = data
            self.ds = xr.open_dataset(data)

        # Image related parameters
        self.startingX = self.ds.x.values[0]
        self.startingY = self.ds.y.values[0]

        self.XSize = self.ds.x.values[1] - self.ds.x.values[0]
        self.YSize = self.ds.y.values[1] - self.ds.y.values[0]

        self.numberOfSamples = len(self.ds.x)
        self.numberOfLines = len(self.ds.y)

        self.ij_epsg = int(self.ds.UTM_Projection.spatial_epsg)
        self.xy_epsg = output_projection

        # Compute bounding box in source projection
        self.bbox_x, self.bbox_y = ItsLiveReproject.bounding_box(
            self.ds,
            self.XSize,
            self.YSize
        )
        self.logger.info(f"P_in bounding box: x: {self.bbox_x} y: {self.bbox_y}")

    @staticmethod
    def bounding_box(ds, dx, dy):
        """
        Select bounding box for the dataset.
        """
        center_off_X = dx/2
        center_off_Y = dy/2

        # Compute cell boundaries as ITS_LIVE grid stores x/y for the cell centers
        xmin = ds.x.values.min() - center_off_X
        xmax = ds.x.values.max() + center_off_X

        # Y coordinate calculations are based on the fact that dy < 0
        ymin = ds.y.values.min() + center_off_Y
        ymax = ds.y.values.max() - center_off_Y

        # ATTN: Assuming that X and Y cell dimensions are the same
        assert np.abs(dx) == np.abs(dy), f"Cell dimensions differ: x={np.abs(dx)} y={np.abs(dy)}"

        return Grid.bounding_box(
            Bounds(min_value=xmin, max_value=xmax),
            Bounds(min_value=ymin, max_value=ymax),
            dx
        )

    def run(self, output_file: str = None):
        """
        Run reprojection of ITS_LIVE granule into target projection.

        This methods warps X and Y components of v and vp velocities.
        """
        self.create_transformation_matrix()

        # outputBounds --- output bounds as (minX, minY, maxX, maxY) in target SRS
        warp_options = gdal.WarpOptions(
            # format='netCDF',
            format='vrt',   # Use virtual memory format to avoid writing warped dataset to the file
            outputBounds=(self.x0_bbox.min, self.y0_bbox.max, self.x0_bbox.max, self.y0_bbox.min),
            xRes=self.XSize,
            yRes=self.YSize,
            srcSRS=f'EPSG:{self.ij_epsg}',
            dstSRS=f'EPSG:{self.xy_epsg}'
        )
        dataset = gdal.Open(f'NETCDF:"{self.input_file}":vx')
        # Warp data variable
        vx_ds = gdal.Warp('', dataset, options=warp_options)
        np_ds = vx_ds.ReadAsArray()
        # print(f"vx_ds.shape = {np_ds.shape}")

        # Apply transformation matrix to vx, vy, vxp, vyp, then compute v, vp

    def create_transformation_matrix(self):
        """
        Reproject variables in ITS_LIVE granule into new projection.
        """
        # Project the bounding box into output projection
        input_projection = osr.SpatialReference()
        input_projection.ImportFromEPSG(self.ij_epsg)

        output_projection = osr.SpatialReference()
        output_projection.ImportFromEPSG(self.xy_epsg)

        ij_to_xy_transfer = osr.CoordinateTransformation(input_projection, output_projection)
        xy_to_ij_transfer = osr.CoordinateTransformation(output_projection, input_projection)

        # Re-project bounding box to output projection
        self.logger.info(f"Reprojecting from {self.ij_epsg} to {self.xy_epsg}")
        points_in = np.array([
            [self.bbox_x.min, self.bbox_y.max],
            [self.bbox_x.max, self.bbox_y.max],
            [self.bbox_x.max, self.bbox_y.min],
            [self.bbox_x.min, self.bbox_y.min]
        ])
        points_out = ij_to_xy_transfer.TransformPoints(points_in)

        bbox_out_x = Bounds([each[0] for each in points_out])
        bbox_out_y = Bounds([each[1] for each in points_out])

        # Get corresponding bounding box in output projection based on edge points of
        # bounding polygon in P_in projection
        self.x0_bbox, self.y0_bbox = Grid.bounding_box(bbox_out_x, bbox_out_y, self.XSize)
        self.logger.info(f"P_out bounding box: x: {self.x0_bbox} y: {self.y0_bbox}")

        # Output grid will be used as input to the gdal.warp() and to identify
        # corresponding grid cells in original P_in projection when computing
        # transformation matrix
        x0_grid, y0_grid = Grid.create(self.x0_bbox, self.y0_bbox, self.XSize)
        self.logger.info(f"Grid in P_out (cell centers): num_x={len(x0_grid)} num_y={len(y0_grid)}")

        xy0_points = ItsLiveReproject.dims_to_grid(x0_grid, y0_grid)
        ij0_points = xy_to_ij_transfer.TransformPoints(xy0_points)
        self.logger.info(f"Len of (x0, y0) points in P_out: {xy0_points.shape}")
        self.logger.info(f"Len of (i0, j0) points in P_in:  {len(ij0_points)}")

        # Calculate x unit vector: add unit length to ij0_points.x
        ij_x_unit = np.array(ij0_points.copy())
        ij_x_unit[:, 0] += self.XSize
        xy1_points = ij_to_xy_transfer.TransformPoints(ij_x_unit.tolist())
        # x1 = [each[0] for each in points_out]
        # y1 = [each[1] for each in points_out]

        # Calculate y unit vector: add unit length to ij0_points.y
        ij_y_unit = np.array(ij0_points.copy())
        ij_y_unit[:, 1] += self.YSize
        xy2_points = ij_to_xy_transfer.TransformPoints(ij_y_unit.tolist())

        # Compute unit vectors based on xy0_points, xy1_points and xy2_points
        # in output projection
        xunit = np.zeros((len(xy0_points), 3))
        yunit = np.zeros((len(xy0_points), 3))

        # Compute unit vector for each cell of the output grid
        for index in range(len(xy0_points)):
            diff = np.array(xy1_points[index]) - np.array(xy0_points[index])
            xunit[index] = diff / np.linalg.norm(diff)

            diff = np.array(xy2_points[index]) - np.array(xy0_points[index])
            yunit[index] = diff / np.linalg.norm(diff)

        print("x_unit[0]: ", xunit[0])
        print("y_unit[0]: ", yunit[0])

        # Local normal vector
        normal = np.array([0.0, 0.0, -1.0])

        # Compute transformation matrix per cell
        transformation_matrix = np.zeros((len(xy0_points)), dtype=np.object)

        # Counter of how many points don't have transformation matrix
        no_value_counter = 0

        # For each point on the output grid:
        for each_index in range(len(xy0_points)):
            # Find corresponding point in P_in projection
            ij_point = ij0_points[each_index]

            # Check if the point in P_in projection is within original granule's
            # X/Y range
            if ij_point[0] < self.bbox_x.min or ij_point[0] > self.bbox_y.max or \
               ij_point[1] < self.bbox_y.min or ij_point[1] > self.bbox_y.max:
                transformation_matrix[each_index] = ItsLiveReproject.NODATA_VALUE
                no_value_counter += 1
                continue

            # Computed normal vector for xunit and yunit at the point
            cross = np.cross(xunit[each_index], yunit[each_index])
            cross = cross / np.linalg.norm(cross)
            cross_check = np.abs(180.0*np.arccos(np.dot(normal, cross))/np.pi)

            # Allow for angular separation less than 1 degree
            if cross_check > 1.0:
                transformation_matrix[each_index] = ItsLiveReproject.NODATA_VALUE
                no_value_counter += 1
                self.logger.info(f"No value due to cross: {cross} for xunit={xunit[each_index]} yunit={yunit[each_index]} vs. normal={normal}")

            else:
                raster1a = normal[2]/(ItsLiveReproject.TIME_DELTA/self.XSize/365.0/24.0/3600.0)*(normal[2]*yunit[1]-normal[1]*yunit[2])/((normal[2]*xunit[0]-normal[0]*xunit[2])*(normal[2]*yunit[1]-normal[1]*yunit[2])-(normal[2]*yunit[0]-normal[0]*yunit[2])*(normal[2]*xunit[1]-normal[1]*xunit[2]))
                raster1b = -normal[2]/(ItsLiveReproject.TIME_DELTA/self.YSize/365.0/24.0/3600.0)*(normal[2]*xunit[1]-normal[1]*xunit[2])/((normal[2]*xunit[0]-normal[0]*xunit[2])*(normal[2]*yunit[1]-normal[1]*yunit[2])-(normal[2]*yunit[0]-normal[0]*yunit[2])*(normal[2]*xunit[1]-normal[1]*xunit[2]))
                raster2a = -normal[2]/(ItsLiveReproject.TIME_DELTA/self.XSize/365.0/24.0/3600.0)*(normal[2]*yunit[0]-normal[0]*yunit[2])/((normal[2]*xunit[0]-normal[0]*xunit[2])*(normal[2]*yunit[1]-normal[1]*yunit[2])-(normal[2]*yunit[0]-normal[0]*yunit[2])*(normal[2]*xunit[1]-normal[1]*xunit[2]));
                raster2b = normal[2]/(ItsLiveReproject.TIME_DELTA/self.YSize/365.0/24.0/3600.0)*(normal[2]*xunit[0]-normal[0]*xunit[2])/((normal[2]*xunit[0]-normal[0]*xunit[2])*(normal[2]*yunit[1]-normal[1]*yunit[2])-(normal[2]*yunit[0]-normal[0]*yunit[2])*(normal[2]*xunit[1]-normal[1]*xunit[2]));

                transformation_matrix[each_index] = np.array([[raster1a, raster1b], [raster2a, raster2b]])

        self.logger.info(f"Number of points with no transformation matrix: {no_value_counter} out of {len(xy0_points)} points ({no_value_counter/len(xy0_points)*100.0}%)")

    @staticmethod
    def dims_to_grid(x, y):
        """
        Convert x, y dimensions of the dataset into numpy grid array.
        """
        # Use z=0 as TransformPoints calls return 3d point coordinates
        grid = np.zeros((len(x)*len(y), 3))
        print(f"DIMS_TO_GRID: x={len(x)} y={len(y)} grid={len(grid)}")

        num_row = 0
        for each_x in x:
            for each_y in y:
                grid[num_row][0] = each_x
                grid[num_row][1] = each_y
                num_row += 1

        return grid


if __name__ == '__main__':
    """
    Re-project ITS_LIVE granule to the target projection.
    """
    parser = argparse.ArgumentParser(description='Re-project ITS_LIVE granule to new projection.')
    parser.add_argument(
        '-i', '--input',
        dest='input_file',
        type=str,
        required=True,
        help='Input file name for ITS_LIVE format data')
    parser.add_argument(
        '-p', '--projection',
        dest='output_proj',
        type=int,
        required=True, help='Output projection')
    parser.add_argument(
        '-o', '--output',
        dest='output_file',
        type=str,
        required=True, help='Output filename to store re-projected granule in target projection')

    command_args = parser.parse_args()

    its_data = ItsLiveReproject(command_args.input_file, command_args.output_proj)
    its_data.run(command_args.output_file)
