import os
import sys
import importlib
import open3d as o3d
import argparse
import logging
import time
import torch
from utils import exp_util, vis_util
from network import utility
import numpy as np
from system import map_surface as surface_map
from system import map_color as color_map

import system.tracker_surface 

from utils import motion_util
from pyquaternion import Quaternion

import pdb

vis_param = argparse.Namespace()
vis_param.n_left_steps = 0
vis_param.args = None
vis_param.mesh_updated = True

vis_param.whole_pc = np.zeros((0,3))



def key_continue(vis):
    vis_param.n_left_steps += 20000
    return False


st = None
prev_rgbd = None
pose = None
def refresh(vis):
    global st, prev_rgbd, pose, pc
    it_st = time.time()
    if vis_param.sequence.frame_id % vis_param.args.integrate_interval == 0:
        st = time.time()
    if vis:
        pass
        # This spares slots for meshing thread to emit commands.
        #time.sleep(0.02)


    if not vis_param.mesh_updated and vis_param.args.run_async:
        st = time.time()
        map_mesh = vis_param.map.extract_mesh(vis_param.args.resolution, 0, extract_async=True)
        if map_mesh is not None:
            vis_param.mesh_updated = True
            o3d.io.write_triangle_mesh(args.outdir+'/recons.ply', map_mesh)
            print('save mesh', time.time()-st)


    if vis_param.n_left_steps == 0:
        return False
    if vis_param.sequence.frame_id >= len(vis_param.sequence):
        # finished, not save model
        print('saving to', vis_param.args.outdir+'/nslfs.pt')
        vis_param.color_map.save_nslfs(vis_param.args.outdir+'/nslfs.pt')
        '''
        with open('tmp.npy', 'wb') as f:
            np.save(f, vis_param.whole_pc)
        '''
        
        if vis:
            return False
        else:
            raise StopIteration
    '''
    if vis_param.sequence.frame_id % 200 == 0:
        # finished, not save model
        print('saving to', vis_param.args.outdir+'/nslfs.pt')
        vis_param.color_map.save_nslfs(vis_param.args.outdir+'/nslfs.pt')
    '''
        


    vis_param.n_left_steps -= 1

    logging.info(f"Frame ID = {vis_param.sequence.frame_id}")
    frame_data = next(vis_param.sequence)

    if (vis_param.sequence.frame_id - 1) % vis_param.args.color_integrate_interval != 0:
        return False



        
    #if (vis_param.sequence.frame_id - 1) % vis_param.args.color_integrate_interval != 0:
    #    return False

    # Prune invalid depths
    frame_data.depth[torch.logical_or(frame_data.depth < vis_param.args.depth_cut_min,
                                      frame_data.depth > vis_param.args.depth_cut_max)] = np.nan
    # Do tracking.
    st = time.time()
    if vis_param.sequence.gt_trajectory is not None:
        frame_pose = vis_param.tracker.track_camera(frame_data.rgb, frame_data.depth, frame_data.calib, vis_param.sequence.gt_trajectory[vis_param.sequence.frame_id-1])
    else:
        frame_pose = vis_param.tracker.track_camera(frame_data.rgb, frame_data.depth, frame_data.calib, vis_param.sequence.first_iso if len(vis_param.tracker.all_pd_pose) == 0 else None)
        '''
        # KITTI save pcd for track
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(vis_param.tracker.tmp_pc[:,:3].cpu().numpy())
        pcd.colors = o3d.utility.Vector3dVector(vis_param.tracker.tmp_rgb[:,:3].cpu().numpy())
        o3d.io.write_point_cloud("outdir/KITTI_depth/pcd/%d.ply"%vis_param.sequence.frame_id, pcd)
        return False
        '''

    print('track time:', time.time()-st, "now takes", time.time()-it_st)

    calib = frame_data.calib
    tracker_pc, tracker_normal = vis_param.tracker.last_processed_pc_color_use
    _, tracker_color = vis_param.tracker.last_colored_pcd

    info = (frame_pose, [calib.fx,calib.fy,calib.cx,calib.cy],tracker_color, frame_data.depth)
    if (vis_param.sequence.frame_id - 1) % vis_param.args.color_integrate_interval == 0:
    # integrate into color 
        opt_depth = frame_pose @ tracker_pc
        
        opt_normal = None#opt_normal = frame_pose.rotation @ tracker_normal
        vis_param.color_map.integrate_keyframe(opt_depth, opt_normal, async_optimize=vis_param.args.run_async,
                                         do_optimize=False, info=info)
        vis_param.whole_pc = np.concatenate([vis_param.whole_pc,opt_depth.cpu().numpy()],axis=0)
    tracker_pc, tracker_normal = vis_param.tracker.last_processed_pc
    # integrate into shape   
    if frame_pose is None: # bad frame in track
        return True
    if (vis_param.sequence.frame_id - 1) % vis_param.args.integrate_interval == 0:
        st = time.time()

        opt_depth = frame_pose @ tracker_pc
        opt_normal = frame_pose.rotation @ tracker_normal
        vis_param.map.integrate_keyframe(opt_depth, opt_normal, async_optimize=vis_param.args.run_async,
                                         do_optimize=True)
        print('integrate difusion', time.time()-st, "now takes", time.time()-it_st)
    if (vis_param.sequence.frame_id - 1) % vis_param.args.meshing_interval == 0:
        # extract mesh
        torch.cuda.empty_cache()
        map_mesh = vis_param.map.extract_mesh(vis_param.args.resolution, int(4e6), max_std=0.15,
                                                  extract_async=vis_param.args.run_async, interpolate=True)
        print('extract mesh', time.time()-st)
        vis_param.mesh_updated = map_mesh is not None
        '''
        print('saving to', vis_param.args.nslfs_path)
        vis_param.color_map.save_nslfs(vis_param.args.nslfs_path)
        with open(args.outdir+'/tag.txt', 'w') as f:
                f.write('!%d!'%(vis_param.sequence.frame_id-1))
        print('save pt', time.time()-st)
        '''

    prev_frame = frame_data
    print('################### This iter takes %f seconds'%(time.time()-it_st))
    return True


if __name__ == '__main__':
    parser = exp_util.ArgumentParserX()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    o3d.utility.set_verbosity_level(o3d.utility.VerbosityLevel.Error)

    # Load in network.  (args.model is the network specification)
    model, args_model = utility.load_model(args.training_hypers, args.using_epoch)
    args.model = args_model
    args.mapping = exp_util.dict_to_args(args.mapping)
    args.color_mapping = exp_util.dict_to_args(args.color_mapping)
    args.tracking = exp_util.dict_to_args(args.tracking)

    # Load in sequence.
    seq_package, seq_class = args.sequence_type.split(".")
    sequence_module = importlib.import_module("dataset.production." + seq_package)
    sequence_module = getattr(sequence_module, seq_class)
    vis_param.sequence = sequence_module(**args.sequence_kwargs)

    # Mapping
    if torch.cuda.device_count() > 1:
        main_device, aux_device = torch.device("cuda", index=0), torch.device("cuda", index=1)
    elif torch.cuda.device_count() == 1:
        main_device, aux_device = torch.device("cuda", index=0), None
    else:
        assert False, "You must have one GPU."

    vis_param.color_map = color_map.DenseIndexedMap(None, args.color_mapping, 10, main_device,
                                        args.run_async, aux_device)
    vis_param.map = surface_map.DenseIndexedMap(model, args.mapping,  args.model.code_length, main_device,
                                        args.run_async, aux_device)
    vis_param.tracker = system.tracker_surface.SDFTracker(vis_param.map, args.tracking)
    vis_param.args = args
    os.makedirs(args.outdir,  exist_ok = True)


    key_continue(None)
    try:
        while True:
            refresh(None)
    except StopIteration:
        #vis_param.color_map.close_nslfs()
        '''
        for id in vis_param.color_map.nslfs:
            vis_param.color_map.nslfs[id].maintenance_thread.join()
        '''
        assert False, "stopped"
        #os._exit(0)
        
