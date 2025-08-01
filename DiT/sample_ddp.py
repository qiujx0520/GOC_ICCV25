# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Samples a large number of images from a pre-trained DiT model using DDP.
Subsequently saves a .npz file that can be used to compute FID and other
evaluation metrics via the ADM repo: https://github.com/openai/guided-diffusion/tree/main/evaluations

For a simple single-GPU/CPU sampling script, see sample.py.
"""
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "4"
import torch
import torch.distributed as dist
from download import find_model
from diffusion import create_diffusion
from diffusers.models import AutoencoderKL
from tqdm import tqdm

from PIL import Image
import numpy as np
import math
import argparse


def create_npz_from_sample_folder(sample_dir, num=50_000):
    """
    Builds a single .npz file from a folder of .png samples.
    """
    samples = []
    for i in tqdm(range(num), desc="Building .npz file from samples"):
        sample_pil = Image.open(f"{sample_dir}/{i:06d}.png")
        sample_np = np.asarray(sample_pil).astype(np.uint8)
        samples.append(sample_np)
    samples = np.stack(samples)
    assert samples.shape == (num, samples.shape[1], samples.shape[2], 3)
    npz_path = f"{sample_dir}.npz"
    np.savez(npz_path, arr_0=samples)
    print(f"Saved .npz file to {npz_path} [shape={samples.shape}].")
    return npz_path



def main(args):
    """
    Run sampling.
    """
    torch.backends.cuda.matmul.allow_tf32 = args.tf32  # True: fast but may lead to some small numerical differences
    assert torch.cuda.is_available(), "Sampling with DDP requires at least one GPU. sample.py supports CPU-only usage"
    torch.set_grad_enabled(False)

    # Setup DDP:
    # dist.init_process_group("nccl")
    # rank = dist.get_rank()
    # device = rank % torch.cuda.device_count()
    # # device = args.device
    # seed = args.global_seed * dist.get_world_size() + rank
    # torch.manual_seed(seed)
    # torch.cuda.set_device(device)
    # # print(f"Starting rank={rank}, seed={seed}, world_size={dist.get_world_size()}.")
    # print(f"Starting rank={rank}, seed={seed}, world_size={dist.get_world_size()}, device={device}.")

    dist.init_process_group("nccl")
    rank = dist.get_rank()
    device = rank % torch.cuda.device_count()
    seed = args.global_seed * dist.get_world_size() + rank
    torch.manual_seed(seed)
    torch.cuda.set_device(device)
    print(f"Starting rank={rank}, seed={seed}, world_size={dist.get_world_size()}.")
    
    if args.ckpt is None:
        assert args.model == "DiT-XL/2", "Only DiT-XL/2 models are available for auto-download."
        assert args.image_size in [256, 512]
        assert args.num_classes == 1000

    diffusion = create_diffusion(str(args.num_sampling_steps))
    
    # Load model:
    latent_size = args.image_size // 8
    if args.accelerate_method == "cache":
        from models.cache_models import DiT_models
    elif args.accelerate_method == "iterate":
        from models.iterate_models import DiT_models
    elif args.accelerate_method == "nolastlayer":
        from models.nolastlayer_models import DiT_models
    elif args.accelerate_method is not None and "ranklayer" in args.accelerate_method:
        from models.rankdrop_models import DiT_models
    elif args.accelerate_method is not None and "bottomlayer" in args.accelerate_method:
        from models.bottom_models import DiT_models
    elif args.accelerate_method is not None and "randomlayer" in args.accelerate_method:
        from models.randomlayer_models import DiT_models
    elif args.accelerate_method is not None and "fixlayer" in args.accelerate_method:
        from models.fixlayer_models import DiT_models
    elif args.accelerate_method is not None and args.accelerate_method == "dynamiclayer":
        from models.dynamic_models import DiT_models
    elif args.accelerate_method is not None and args.accelerate_method == "layerdropout":
        from models.layerdropout_models import DiT_models
    elif args.accelerate_method is not None and args.accelerate_method == "dynamiclayer_soft":
        from models.router_models_inference import DiT_models
    else:
        from models.models import DiT_models

    model = DiT_models[args.model](
        input_size=latent_size,
        num_classes=args.num_classes
    ).to(device)

    if args.accelerate_method is not None:
        if 'ranklayer' in args.accelerate_method:
            model.load_ranking(args.num_sampling_steps, args.accelerate_method)
        elif 'randomlayer' in args.accelerate_method:
            model.load_ranking(args.accelerate_method)
        elif 'bottomlayer' in args.accelerate_method or 'fixlayer' in args.accelerate_method:
            model.load_ranking(args.accelerate_method)
        elif 'dynamiclayer' in args.accelerate_method or 'layerdropout' in args.accelerate_method or 'dynamiclayer_soft' in args.accelerate_method:
            model.load_ranking(args.path, args.num_sampling_steps, diffusion.timestep_map, args.thres)
    
    
    # Auto-download a pre-trained model or load a custom DiT checkpoint from train.py:
    ckpt_path = args.ckpt or f"DiT-XL-2-{args.image_size}x{args.image_size}.pt"
    state_dict = find_model(ckpt_path)
    model.load_state_dict(state_dict)
    model.eval()  # important!
    vae = AutoencoderKL.from_pretrained(f"~/GOC/DiT/pretrained_models/sd-vae-ft-ema").to(device)
    assert args.cfg_scale >= 1.0, "In almost all cases, cfg_scale be >= 1.0"
    using_cfg = args.cfg_scale > 1.0

    # Create folder to save samples:
    model_string_name = args.model.replace("/", "-")
    ckpt_string_name = os.path.basename(args.ckpt).replace(".pt", "") if args.ckpt else "pretrained"
    if args.accelerate_method is not None and 'dynamiclayer' in args.accelerate_method:
        router_name = args.path.split('/')[1].split('.')[0]
        folder_name = f"router-{router_name}-thres-{args.thres}-accelerate-{args.accelerate_method}-size-{args.image_size}-vae-{args.vae}-ddim-{args.ddim_sample}-" \
                      f"steps-{args.num_sampling_steps}-cfg-{args.cfg_scale}-seed-{args.global_seed}"
    else:
        folder_name = f"{model_string_name}-{ckpt_string_name}-size-{args.image_size}-vae-{args.vae}-psampler-{args.p_sample}-ddim-{args.ddim_sample}-" \
                  f"steps-{args.num_sampling_steps}-accelerate-{args.accelerate_method}-cfg-{args.cfg_scale}-seed-{args.global_seed}"
    sample_folder_dir = f"{args.sample_dir}/{folder_name}"

    os.makedirs(f"{args.sample_dir}", exist_ok=True)
    if rank == 0 and args.save_to_disk:
        os.makedirs(sample_folder_dir, exist_ok=True)
        print(f"Saving .png samples at {sample_folder_dir}")
    dist.barrier()

    # Figure out how many samples we need to generate on each GPU and how many iterations we need to run:
    n = args.per_proc_batch_size
    global_batch_size = n * dist.get_world_size()
    # To make things evenly-divisible, we'll sample a bit more than we need and then discard the extra samples:
    total_samples = int(math.ceil(args.num_fid_samples / global_batch_size) * global_batch_size)
    if rank == 0:
        print(f"Total number of images that will be sampled: {total_samples}")
        all_images = []

    assert total_samples % dist.get_world_size() == 0, "total_samples must be divisible by world_size"
    samples_needed_this_gpu = int(total_samples // dist.get_world_size())
    assert samples_needed_this_gpu % n == 0, "samples_needed_this_gpu must be divisible by the per-GPU batch size"
    iterations = int(samples_needed_this_gpu // n)
    pbar = range(iterations)
    pbar = tqdm(pbar) if rank == 0 else pbar
    total = 0
    
    current_step = 0
    
    for _ in pbar:
        # model.reset(args.num_sampling_steps)
        model.reset()
        
        # Sample inputs:
        z = torch.randn(n, model.in_channels, latent_size, latent_size, device=device)
        y = torch.randint(0, args.num_classes, (n,), device=device)
        

        # Setup classifier-free guidance:
        if using_cfg:
            z = torch.cat([z, z], 0)
            y_null = torch.tensor([1000] * n, device=device)
            y = torch.cat([y, y_null], 0)
            model_kwargs = dict(y=y, cfg_scale=args.cfg_scale)
            sample_fn = model.forward_with_cfg
        else:
            model_kwargs = dict(y=y)
            sample_fn = model.forward

        # block_outputs = torch.load('~/GOC/DiT/averaged_pth/averaged_block_outputs.pth')
        block_outputs = torch.load('~/GOC/DiT/averaged_block_outputs.pth')
        
        # Sample images:
        if args.p_sample:
            samples = diffusion.p_sample_loop(
                sample_fn, z.shape, z, clip_denoised=False, model_kwargs=model_kwargs, progress=False, device=device
            )
        elif args.ddim_sample:
            # samples = diffusion.ddim_sample_loop(
            #     sample_fn, z.shape, z, clip_denoised=False, model_kwargs=model_kwargs, progress=False, device=device
            # )
            # samples = diffusion.ddim_sample_loop(
            #     sample_fn, z.shape, z, current_step, clip_denoised=False, model_kwargs=model_kwargs, progress=True, device=device
            # )
            samples = diffusion.ddim_sample_loop(
                model.forward_with_cfg, z.shape, z, clip_denoised=False, model_kwargs=model_kwargs, progress=True, device=device, block_outputs=block_outputs
            )
        else:
            raise NotImplementedError
        
        if using_cfg:
            samples, _ = samples.chunk(2, dim=0)  # Remove null class samples

        samples = vae.decode(samples / 0.18215).sample
        samples = torch.clamp(127.5 * samples + 128.0, 0, 255).permute(0, 2, 3, 1).to(dtype=torch.uint8)

        # Save samples to disk as individual .png files
        if args.save_to_disk:
            for i, sample in enumerate(samples):
                index = i * dist.get_world_size() + rank + total
                sample = sample.cpu().numpy()
                Image.fromarray(sample).save(f"{sample_folder_dir}/{index:06d}.png")
        else:
            samples = samples.contiguous()
            gathered_samples = [torch.zeros_like(samples) for _ in range(dist.get_world_size())]
            dist.all_gather(gathered_samples, samples) 

            if rank == 0:
                all_images.extend([sample.cpu().numpy() for sample in gathered_samples])
        total += global_batch_size

        dist.barrier()

    # Make sure all processes have finished saving their samples before attempting to convert to .npz
    dist.barrier()
    if rank == 0:
        if args.save_to_disk:
            create_npz_from_sample_folder(sample_folder_dir, args.num_fid_samples)
            print("Done.")
        else:
            if rank == 0:
                arr = np.concatenate(all_images, axis=0)
                arr = arr[: args.num_fid_samples]

                out_path =  f"{sample_folder_dir}.npz"

                print(f"saving to {out_path}")
                np.savez(out_path, arr_0=arr)
    dist.barrier()
    dist.destroy_process_group()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, default="DiT-XL/2")
    parser.add_argument("--vae",  type=str, choices=["ema", "mse"], default="ema")
    parser.add_argument("--sample-dir", type=str, default="samples")
    parser.add_argument("--per-proc-batch-size", type=int, default=32)
    parser.add_argument("--num-fid-samples", type=int, default=50_000)
    parser.add_argument("--image-size", type=int, choices=[256, 512], default=256)
    parser.add_argument("--num-classes", type=int, default=1000)
    parser.add_argument("--cfg-scale",  type=float, default=1.5)
    parser.add_argument("--num-sampling-steps", type=int, default=250)
    parser.add_argument("--global-seed", type=int, default=0)
    parser.add_argument("--tf32", action=argparse.BooleanOptionalAction, default=True,
                        help="By default, use TF32 matmuls. This massively accelerates sampling on Ampere GPUs.")
    parser.add_argument("--ckpt", type=str, default=None,
                        help="Optional path to a DiT checkpoint (default: auto-download a pre-trained DiT-XL/2 model).")
    
    parser.add_argument("--ddim-sample", action="store_true", default=False,)
    parser.add_argument("--p-sample", action="store_true", default=False,)

    parser.add_argument("--accelerate-method", type=str, default=None,
                        help="Use the accelerated version of the model.")
    parser.add_argument("--thres", type=float, default=0.5)
    
    parser.add_argument("--name", type=str, default="None")
    parser.add_argument("--path", type=str, default=None,)

    parser.add_argument("--save-to-disk", action="store_true", default=False,)
    # parser.add_argument("--device", type=int, default=7, help="CUDA device to use (default: 0)")

    args = parser.parse_args()
    main(args)
