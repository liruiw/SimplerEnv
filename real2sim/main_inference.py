import numpy as np
import os
import tensorflow as tf
import argparse

from transforms3d.euler import euler2axangle, euler2quat
from sapien.core import Pose

from real2sim.rt1.rt1_model import RT1Inference
from real2sim.octo.octo_model import OctoInference
from real2sim.utils.visualization import write_video
from real2sim.utils.env.env_builder import build_maniskill2_env, get_robot_control_mode
from real2sim.utils.env.additional_episode_stats import (
    initialize_additional_episode_stats, update_additional_episode_stats, obtain_truncation_step_success
)
from real2sim.utils.io import DictAction

def main(model, ckpt_path, robot_name, env_name, scene_name, 
         robot_init_x, robot_init_y, robot_init_quat, 
         obj_init_x, obj_init_y,
         control_mode,
         additional_env_build_kwargs={},
         rgb_overlay_path=None, tmp_exp=False,
         control_freq=3, sim_freq=513, max_episode_steps=80,
         instruction=None,
         action_scale=1.0):
    
    # Create environment
    env, task_description = build_maniskill2_env(
                env_name,
                obs_mode='rgbd',
                robot=robot_name,
                sim_freq=sim_freq,
                control_mode=control_mode,
                control_freq=control_freq,
                max_episode_steps=max_episode_steps,
                scene_name=scene_name,
                camera_cfgs={"add_segmentation": True},
                rgb_overlay_path=rgb_overlay_path,
                instruction=instruction,
                **additional_env_build_kwargs,
                # Enable Ray Tracing
                # shader_dir="rt",
                # render_config={"rt_samples_per_pixel": 8, "rt_use_denoiser": True},
    )
    env_reset_options = {
        'obj_init_options': {
            'init_xy': np.array([obj_init_x, obj_init_y]),
        },
        'robot_init_options': {
            'init_xy': np.array([robot_init_x, robot_init_y]),
            'init_rot_quat': robot_init_quat,
        }
    }
    
    # Reset and initialize environment
    obs, _ = env.reset(options=env_reset_options)
    image = obs['image']['overhead_camera']['rgb']
    images = [image]
    predicted_actions = []
    predicted_terminated, done, truncated = False, False, False
    
    model.reset(task_description)
    additional_episode_stats = initialize_additional_episode_stats(env_name)
        
    timestep = 0
    success = "failure"
    
    # Step the environment
    while not (predicted_terminated or truncated):
        cur_gripper_closedness = env.agent.get_gripper_closedness()
        
        raw_action, action = model.step(image, cur_gripper_closedness)
        predicted_actions.append(raw_action)
        print(timestep, raw_action)
        predicted_terminated = bool(action['terminate_episode'][0] > 0)
        
        obs, reward, done, truncated, info = env.step(
            np.concatenate(
                [action['world_vector'], 
                action['rot_axangle'],
                action['gripper']
                ]
            )
        )
        additional_episode_stats = update_additional_episode_stats(env_name, additional_episode_stats, info)
        if predicted_terminated and info['success']:
            success = "success"
        
        image = obs['image']['overhead_camera']['rgb']
        images.append(image)
        timestep += 1
        print(info)

    # obtain success indicator if policy never terminates
    if obtain_truncation_step_success(env_name, additional_episode_stats, info):
        success = "success"
    
    # save video
    env_save_name = env_name
    for k, v in additional_env_build_kwargs.items():
        env_save_name = env_save_name + f'_{k}_{v}'
    ckpt_path_basename = ckpt_path if ckpt_path[-1] != '/' else ckpt_path[:-1]
    ckpt_path_basename = ckpt_path_basename.split('/')[-1]
    video_name = f'{success}_obj_{obj_init_x}_{obj_init_y}'
    for k, v in additional_episode_stats.items():
        video_name = video_name + f'_{k}_{v}'
    video_name = video_name + '.mp4'
    if rgb_overlay_path is not None:
        rgb_overlay_path_str = os.path.splitext(os.path.basename(rgb_overlay_path))[0]
    else:
        rgb_overlay_path_str = 'None'
    video_path = f'{ckpt_path_basename}/{scene_name}/{control_mode}/{env_save_name}/rob_{robot_init_x}_{robot_init_y}_rgb_overlay_{rgb_overlay_path_str}/{video_name}'
    if not tmp_exp:
        video_path = 'results/' + video_path
    else:
        video_path = 'results_tmp/' + video_path 
    write_video(video_path, images, fps=5)
    
    # save action trajectory
    action_path = video_path.replace('.mp4', '.png')
    action_root = os.path.dirname(action_path) + '/actions/'
    os.makedirs(action_root, exist_ok=True)
    action_path = action_root + os.path.basename(action_path)
    model.visualize_epoch(predicted_actions, images, save_path=action_path)
    

def parse_range_tuple(t):
    return np.linspace(t[0], t[1], int(t[2]))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--policy-model', type=str, default='rt1', choices=['rt1', 'octo-base', 'octo-small'])
    parser.add_argument('--ckpt-path', type=str, required=True)
    parser.add_argument('--env-name', type=str, required=True)
    parser.add_argument('--scene-name', type=str, default='google_pick_coke_can_1_v4')
    parser.add_argument('--robot', type=str, default='google_robot_static')
    parser.add_argument('--gpu-id', type=int, default=0)
    parser.add_argument('--action-scale', type=float, default=1.0)
    
    parser.add_argument('--control-freq', type=int, default=3)
    parser.add_argument('--sim-freq', type=int, default=513)
    parser.add_argument('--max-episode-steps', type=int, default=80)
    parser.add_argument('--rgb-overlay-path', type=str, default=None)
    parser.add_argument('--robot-init-x-range', type=float, nargs=3, default=[0.35, 0.35, 1], help="[xmin, xmax, num]")
    parser.add_argument('--robot-init-y-range', type=float, nargs=3, default=[0.20, 0.20, 1], help="[ymin, ymax, num]")
    parser.add_argument('--robot-init-rot-quat-center', type=float, nargs=4, default=[1, 0, 0, 0], help="[x, y, z, w]")
    parser.add_argument('--robot-init-rot-rpy-range', type=float, nargs=9, default=[0, 0, 1, 0, 0, 1, 0, 0, 1], 
                        help="[rmin, rmax, rnum, pmin, pmax, pnum, ymin, ymax, ynum]")
    parser.add_argument('--obj-init-x-range', type=float, nargs=3, default=[-0.35, -0.12, 5], help="[xmin, xmax, num]")
    parser.add_argument('--obj-init-y-range', type=float, nargs=3, default=[-0.02, 0.42, 5], help="[ymin, ymax, num]")
    
    parser.add_argument("--additional-env-build-kwargs", nargs="+", action=DictAction,
        help="Additional env build kwargs in xxx=yyy format. If the value "
        'is a list, it should be like key="[a,b]" or key=a,b '
        'It also allows nested list/tuple values, e.g. key="[(a,b),(c,d)]" '
        "Note that the quotation marks are necessary and that no white space "
        "is allowed.",
    )
    
    args = parser.parse_args()
    
    os.environ['CUDA_VISIBLE_DEVICES'] = f'{args.gpu_id}'
    os.environ['DISPLAY'] = ''
    
    if args.policy_model == 'rt1':
        gpus = tf.config.list_physical_devices('GPU')
        tf.config.set_logical_device_configuration(
            gpus[0],
            [tf.config.LogicalDeviceConfiguration(memory_limit=4096)])
      
    # env args
    control_mode = get_robot_control_mode(args.robot)
    control_freq, sim_freq, max_episode_steps = args.control_freq, args.sim_freq, args.max_episode_steps
    robot_init_xs = parse_range_tuple(args.robot_init_x_range)
    robot_init_ys = parse_range_tuple(args.robot_init_y_range)
    obj_init_xs = parse_range_tuple(args.obj_init_x_range)
    obj_init_ys = parse_range_tuple(args.obj_init_y_range)
    robot_init_quats = []
    for r in parse_range_tuple(args.robot_init_rot_rpy_range[:3]):
        for p in parse_range_tuple(args.robot_init_rot_rpy_range[3:6]):
            for y in parse_range_tuple(args.robot_init_rot_rpy_range[6:]):
                robot_init_quats.append((Pose(q=euler2quat(r, p, y)) * Pose(q=args.robot_init_rot_quat_center)).q)
    additional_env_build_kwargs = args.additional_env_build_kwargs
    
    # policy
    if args.policy_model == 'rt1':
        model = RT1Inference(saved_model_path=args.ckpt_path, action_scale=args.action_scale)
    elif 'octo' in args.policy_model:
        model = OctoInference(model_type=args.policy_model, action_scale=args.action_scale)
    else:
        raise NotImplementedError()
    
    # run inference
    for robot_init_x in robot_init_xs:
        for robot_init_y in robot_init_ys:
            for robot_init_quat in robot_init_quats:
                for obj_init_x in obj_init_xs:
                    for obj_init_y in obj_init_ys:
                        main(model, args.ckpt_path, args.robot, args.env_name, args.scene_name, 
                             robot_init_x, robot_init_y, robot_init_quat, 
                             obj_init_x, obj_init_y,
                             control_mode,
                             additional_env_build_kwargs=additional_env_build_kwargs,
                             rgb_overlay_path=args.rgb_overlay_path,
                             control_freq=control_freq, sim_freq=sim_freq, max_episode_steps=max_episode_steps,
                             action_scale=args.action_scale)
    
"""
# control_mode='arm_pd_ee_delta_pose_align_interpolate_gripper_pd_joint_pos',
# control_mode='arm_pd_ee_delta_pose_align_interpolate_by_planner_gripper_pd_joint_target_pos',
# control_mode='arm_pd_ee_delta_pose_align_interpolate_by_planner_gripper_pd_joint_target_pos_interpolate_by_planner',
# control_mode='arm_pd_ee_delta_pose_align_gripper_pd_joint_target_pos',
# control_mode='arm_pd_ee_delta_pose_align_interpolate_gripper_pd_joint_target_pos',
# control_mode='arm_pd_ee_target_delta_pose_align_interpolate_by_planner_gripper_pd_joint_target_pos',
# control_mode='arm_pd_ee_target_delta_pose_align_interpolate_by_planner_gripper_pd_joint_target_delta_pos_interpolate_by_planner',

# Baked_sc1_staging_table_616385
# robot_init_x, robot_init_y = 0.32, 0.188
# rob_init_quat = (Pose(q=[0, 0, 0, 1]) * Pose(q=euler2quat(0, 0, -0.01))).q
# obj_init_x_range = np.linspace(-0.35, -0.1, 5)
# obj_init_y_range = np.linspace(0.0, 0.4, 5)
# rgb_overlay_path = '/home/xuanlin/Real2Sim/ManiSkill2_real2sim/data/google_table_top_1.png'
# for env_name in ['GraspSingleVerticalCokeCanInScene-v0', 'GraspSingleCokeCanInScene-v0', 'GraspSingleUpRightOpenedCokeCanInScene-v0']:
#     main(env_name, 'Baked_sc1_staging_table_616385', rgb_overlay_path=rgb_overlay_path,
#          obj_init_x_range=obj_init_x_range, obj_init_y_range=obj_init_y_range,
#          robot_init_x=robot_init_x, robot_init_y=robot_init_y, robot_init_quat=rob_init_quat)
"""