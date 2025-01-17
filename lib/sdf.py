import numpy as np


def create_grid(resX, resY, resZ, b_min=np.array([0, 0, 0]), b_max=np.array([1, 1, 1]), transform=None):
    '''
    Create a dense grid of given resolution and bounding box
    :param resX: resolution along X axis
    :param resY: resolution along Y axis
    :param resZ: resolution along Z axis
    :param b_min: vec3 (x_min, y_min, z_min) bounding box corner
    :param b_max: vec3 (x_max, y_max, z_max) bounding box corner
    :return: [3, resX, resY, resZ] coordinates of the grid, and transform matrix from mesh index
    '''
    coords = np.mgrid[:resX, :resY, :resZ] # int64, for WHD idx, (3,resX,resY,resZ), 3 just indicate the XYZ indicies.
    coords = coords.reshape(3, -1) # voxel-space idx tensor, int64, for WHD idx, (3, 256*256*256)

    # coords_matrix: 4x4, {XYZ-scaling, trans} matrix from voxel-space to mesh-coords, by left Mul. with voxel-space idx tensor
    coords_matrix = np.eye(4)
    length = b_max - b_min # np.array([256, 256, 256])

    # scaling factors for XYZ axis between voxel-space and mesh-coords
    coords_matrix[0, 0] = length[0] / resX
    coords_matrix[1, 1] = length[1] / resY
    coords_matrix[2, 2] = length[2] / resZ

    # trans between voxel-space and mesh-coords
    coords_matrix[0:3, 3] = b_min

    # WHD, XYZ, voxel-space converted to mesh-coords
    coords = np.matmul(coords_matrix[:3, :3], coords) + coords_matrix[:3, 3:4] # (3, 256*256*256)

    # transform default: None
    if transform is not None:
        coords = np.matmul(transform[:3, :3], coords) + transform[:3, 3:4]
        coords_matrix = np.matmul(transform, coords_matrix)
    
    # WHD, XYZ, voxel-space converted to mesh-coords, (3, 256, 256, 256)
    coords = coords.reshape(3, resX, resY, resZ)

    return coords, coords_matrix


def batch_eval(points, eval_func, num_samples=512 * 512 * 512):
    """
    input
        points: (3, 256*256*256), XYZ, WHD, in the mesh-coords
    """

    num_pts = points.shape[1] # 256*256*256
    sdf = np.zeros(num_pts)   # 256*256*256

    num_batches = num_pts // num_samples # num of batches

    # for each batch of pts, estimate their occupancy values
    for i in range(num_batches):
        sdf[i * num_samples:i * num_samples + num_samples] = eval_func(points[:, i * num_samples:i * num_samples + num_samples]) # (num_samples,)

    # don't forget the left-over points after batch-inference
    if num_pts % num_samples:
        sdf[num_batches * num_samples:] = eval_func(points[:, num_batches * num_samples:])

    return sdf


def eval_grid(coords, eval_func, num_samples=512 * 512 * 512):
    """
    input
        coords: WHD, XYZ, voxel-space converted to mesh-coords, (3, 256, 256, 256)
        def eval_func(points):
            points  = np.expand_dims(points, axis=0)                   # (1,         3, num_samples)
            points  = np.repeat(points, net.num_views, axis=0)         # (num_views, 3, num_samples)
            samples = torch.from_numpy(points).to(device=cuda).float() # (num_views, 3, num_samples)
            net.query(samples, calib_tensor)                           # calib_tensor is (num_views, 4, 4)
            pred = net.get_preds()[0][0]                               # (num_samples,)
            return pred.detach().cpu().numpy()   
        num_samples: batch_size of points during inference, default 10000
    """

    resolution = coords.shape[1:4] # XYZ, WHD, (256, 256, 256)
    coords     = coords.reshape([3, -1]) # (3, 256*256*256)

    sdf = batch_eval(coords, eval_func, num_samples=num_samples) # (256*256*256,) float 0. ~ 1. for occupancy 
    
    return sdf.reshape(resolution)  # XYZ, WHD, (256, 256, 256), float 0. ~ 1. for occupancy


def eval_grid_octree(coords, eval_func, init_resolution=64, threshold=0.01, num_samples=512 * 512 * 512):
    """
    Note
        1) it's very important to have a not too small init_resolution (namely initial reso can't be too large, 4 is an OKay value), otherwise
        you might wrongly fill up large regions with (min+max)/2 mean values, and thus miss some iso-surfaces. When init_resolution
        equals resolution[0], octree inference degrades to raw grid inference with no speed up.

        2) threshold should be small to assure that the region filling with (min+max)/2 mean is reliable, otherwise e.g. you might wrongly move the iso-surfaces tho.
        you won't miss the surface in between min and max. This is kinda based on a local smoothness assumption, so we need to ensure that the definition of locality is very strict
        by having a very small threshold.

    input
        coords: WHD, XYZ, voxel-space converted to mesh-coords, (3, 256, 256, 256)
        def eval_func(points):
            points  = np.expand_dims(points, axis=0)                   # (1,         3, num_samples)
            points  = np.repeat(points, net.num_views, axis=0)         # (num_views, 3, num_samples)
            samples = torch.from_numpy(points).to(device=cuda).float() # (num_views, 3, num_samples)
            net.query(samples, calib_tensor)                           # calib_tensor is (num_views, 4, 4)
            pred = net.get_preds()[0][0]                               # (num_samples,)
            return pred.detach().cpu().numpy() 
        num_samples: batch_size of points during inference, default 10000
    """

    resolution = coords.shape[1:4] # (256, 256, 256)

    sdf = np.zeros(resolution) # XYZ, WHD, (256, 256, 256)

    # for octree processing
    dirty     = np.ones(resolution, dtype=np.bool_)  # mark voxels that have been tested so far, by False
    grid_mask = np.zeros(resolution, dtype=np.bool_) # mark voxels to be tested in this iteration, by True

    # initial-resolution-scales wrt the-raw-resolution
    reso = resolution[0] // init_resolution # default: 4
    while reso > 0:

        # subdivide the grid
        grid_mask[0:resolution[0]:reso, 0:resolution[1]:reso, 0:resolution[2]:reso] = True

        # test samples in this iteration
        test_mask = np.logical_and(grid_mask, dirty)
        #print('step size:', reso, 'test sample size:', test_mask.sum())
        points = coords[:, test_mask]

        sdf[test_mask] = batch_eval(points, eval_func, num_samples=num_samples)
        dirty[test_mask] = False

        # do interpolation
        if reso <= 1:
            break
        for x in range(0, resolution[0] - reso, reso):
            for y in range(0, resolution[1] - reso, reso):
                for z in range(0, resolution[2] - reso, reso):

                    # if center marked, return
                    if not dirty[x + reso // 2, y + reso // 2, z + reso // 2]:
                        continue

                    v0 = sdf[x, y, z]
                    v1 = sdf[x, y, z + reso]
                    v2 = sdf[x, y + reso, z]
                    v3 = sdf[x, y + reso, z + reso]
                    v4 = sdf[x + reso, y, z]
                    v5 = sdf[x + reso, y, z + reso]
                    v6 = sdf[x + reso, y + reso, z]
                    v7 = sdf[x + reso, y + reso, z + reso]
                    v = np.array([v0, v1, v2, v3, v4, v5, v6, v7])
                    v_min = v.min()
                    v_max = v.max()

                    # this cell is all the same
                    if (v_max - v_min) < threshold:
                        sdf[x:x + reso, y:y + reso, z:z + reso] = (v_max + v_min) / 2 # this can be improved by true linear interpolation, not hard-coded mean values
                        dirty[x:x + reso, y:y + reso, z:z + reso] = False
        reso //= 2

    return sdf.reshape(resolution) # XYZ, WHD, (256, 256, 256), float 0. ~ 1. for occupancy




