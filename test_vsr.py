import argparse
import cv2
import math
import os.path
import torch
import av

from datetime import timedelta
from fractions import Fraction

from utils import utils_image as util
from utils.utils_video import VideoDecoder, VideoEncoder

if not torch.cuda.is_available():
    print('CUDA is not available. Exiting...')
    exit()

default_device = torch.device('cuda')
torch.backends.cudnn.benchmark = True

if torch.cuda.is_bf16_supported():
    default_dtype = torch.bfloat16
else:
    props = torch.cuda.get_device_properties(default_device)
    # fp16 supported at compute 5.3 and above
    if props.major > 5 or (props.major == 5 and props.minor >= 3):
        default_dtype = torch.float16
    else:
        default_dtype = torch.float32

def main():
    n_channels = 3

    # ----------------------------------------
    # Preparation
    # ----------------------------------------
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_path', type=str, default=None, help='path to the model')
    parser.add_argument('--input', type=str, default='input', help='path of inputs')
    parser.add_argument('--output', type=str, default='output', help='path of results')
    parser.add_argument('--depth', type=int, default=16, help='bit depth of outputs')
    parser.add_argument('--suffix', type=str, default=None, help='output filename suffix')
    parser.add_argument('--video', type=str, default=None, help='ffmpeg video codec. if chosen, output video instead of images', choices=['dnxhd', 'h264_nvenc', 'libx264', 'libx265', '...'])
    parser.add_argument('--crf', type=int, default=11, help='video crf')
    parser.add_argument('--preset', type=str, default='slow', help='video preset')
    parser.add_argument('--fps', type=str, default=None, 
                        help='video framerate (defaults to input video\'s frame rate when processing video)')
    parser.add_argument('--res', type=str, default=None, help='video resolution to scale output to (optional, will auto-calculate if not specified)')
    parser.add_argument('--presize', action='store_true', help='resize video before processing')

    args = parser.parse_args()

    if not args.model_path:
        parser.print_help()
        raise ValueError('Please specify model_path')

    model_path = args.model_path
    model_name = os.path.splitext(os.path.basename(model_path))[0]
    
    # ----------------------------------------
    # L_path, E_path
    # ----------------------------------------
    L_path = args.input   # L_path, for Low-quality images
    E_path = args.output  # E_path, for Estimated images

    if not L_path or not os.path.exists(L_path):
        print('Error: input path does not exist.')
        return
    
    video_input = False
    if L_path.split('.')[-1].lower() in ['webm','mkv', 'flv', 'vob', 'ogv', 'ogg', 'drc', 'gif', 'gifv', 'mng', 'avi', 'mts', 'm2ts', 'ts', 'mov', 'qt', 'wmv', 'yuv', 'rm', 'rmvb', 'viv', 'asf', 'amv', 'mp4', 'm4p', 'm4v', 'mpg', 'mp2', 'mpeg', 'mpe', 'mpv', 'm2v', 'm4v', 'svi', '3gp', '3g2', 'mxf', 'roq', 'nsv', 'f4v', 'f4p', 'f4a', 'f4b']:
        video_input = True
        if not args.video:
            print('Error: input video requires --video to be set')
            return
    elif os.path.isdir(L_path):
        L_paths = util.get_image_paths(L_path)
    else:
        L_paths = [L_path]

    if args.video and (not E_path or os.path.isdir(E_path)):
        print('Error: output path must be a single video file')
        return

    if not os.path.exists(E_path) and os.path.splitext(E_path)[1] == '':
        util.mkdir(E_path)
    if not args.video and not os.path.isdir(E_path) and os.path.isdir(L_path):
        E_path = os.path.dirname(E_path)
    
    # ----------------------------------------
    # load model
    # ----------------------------------------
    torch.cuda.empty_cache()

    from models.network_tscunet import TSCUNet as net
    model = net(state=torch.load(model_path))
    model.eval()
    scale = model.scale
    clip_size = model.clip_size

    for k, v in model.named_parameters():
        v.requires_grad = False
    model = model.to(default_device)
    
    input_shape = (1, clip_size, 3, 540, 720)
    dummy_input = torch.randn(input_shape).to(default_device, dtype=default_dtype)

    torch.cuda.empty_cache()
    
    # warmup
    with torch.no_grad():
        with torch.cuda.amp.autocast(dtype=default_dtype):
            _ = model(dummy_input)

    print('Model path: {:s}'.format(model_path))

    print('model_name:{}'.format(model_name))
    print(L_path)

    num_parameters = sum(map(lambda x: x.numel(), model.parameters()))
    print('{:>16s} : {:<.4f} [M]'.format('#Params', num_parameters/10**6))

    if args.suffix:
        suffix = f"{scale}x_{args.suffix}"
    else:
        suffix = f"{model_name}" if f"{scale}x_" in model_name else f"{scale}x_{model_name}"

    if video_input:
        video_decoder = VideoDecoder(L_path, options={'r': '24000/1001' })
        img_count = len(video_decoder)
        video_decoder.start()
        
        # Get first frame to determine input resolution
        first_frame = video_decoder.get_frame()
        input_height, input_width = first_frame.shape[:2]
        # Reset video decoder
        video_decoder.stop()
        
        # Get input video's frame rate to use for output
        with av.open(L_path) as container:
            input_fps = container.streams.video[0].average_rate
        
        # Use the input video's frame rate for decoding
        video_decoder = VideoDecoder(L_path, options={'r': str(input_fps)})
        video_decoder.start()
    else:
        # For image input, get resolution from first image
        if len(L_paths) > 0:
            first_img = util.imread_uint(L_paths[0], n_channels=n_channels)
            input_height, input_width = first_img.shape[:2]
        else:
            print('Error: no input images found.')
            return

    # Calculate output resolution if not manually specified
    if args.res is None:
        if args.presize:
            # If presize is true, output resolution should match input resolution
            output_width = input_width
            output_height = input_height
        else:
            # Otherwise, scale up by model's scale factor
            output_width = input_width * scale
            output_height = input_height * scale
        output_res = f"{output_width}:{output_height}"
    else:
        output_res = args.res

    input_window = []
    image_names = []
    total_time = 0
    end_of_video = False
    video_encoder = None
    try:
        if args.video:
            if args.fps is None and video_input:
                # Use the input video's frame rate if not specified
                fps = input_fps
            elif args.fps is None:
                # Default for non-video inputs
                fps = Fraction(24000, 1001)
            elif '/' in args.fps:
                fps = Fraction(*map(int, args.fps.split('/')))
            elif '.' in args.fps:
                fps = float(args.fps)
            else:
                fps = int(args.fps)

            codec_options = {
                'crf': str(args.crf),
                'preset': args.preset,
            }
            video_encoder = VideoEncoder(
                E_path,
                int(output_res.split(':')[0]),
                int(output_res.split(':')[1]),
                fps=fps,
                codec=args.video,
                options=codec_options,
                input_depth=args.depth,
            )
            video_encoder.start()

        idx = 0
        while True:
            start = torch.cuda.Event(enable_timing=True)
            end = torch.cuda.Event(enable_timing=True)
            start.record()

            # ------------------------------------
            # (1) img_L
            # ------------------------------------
            if video_input:
                img_L = video_decoder.get_frame()
            elif len(L_paths) == 0:
                img_L = None
            else:
                img_L = L_paths.pop(0)
                img_name, ext = os.path.splitext(os.path.basename(img_L))
                img_L = util.imread_uint(img_L, n_channels=n_channels)
                image_names += [img_name]
                
            if img_L is None and not end_of_video:
                img_count = idx + clip_size // 2
                end_of_video = True
                # reflect pad the end of the window
                input_window += input_window[clip_size//2-1:-1][::-1]
            elif not end_of_video:
                if args.presize:
                    img_L = cv2.resize(img_L, (int(output_res.split(':')[0])//scale, int(output_res.split(':')[1])//scale), interpolation=cv2.INTER_CUBIC)
            
                img_L_t = util.uint2tensor4(img_L)
                img_L_t = img_L_t.to(default_device, dtype=default_dtype)

                input_window += [img_L_t]

            if len(input_window) < clip_size and end_of_video:
                # no more frames to process
                break
            elif len(input_window) < clip_size // 2 + 1:
                # wait for more frames
                continue
            elif len(input_window) == clip_size // 2 + 1:
                # reflect pad the beginning of the window
                input_window = input_window[1:][::-1] + input_window

            # ------------------------------------
            # (2) img_E
            # ------------------------------------
            
            #rng_state = torch.get_rng_state()
            #torch.manual_seed(13)
            window = torch.stack(input_window[:clip_size], dim=1)
            
            with torch.cuda.amp.autocast(dtype=default_dtype):
                img_E = model(window)
            #img_E, _ = util.tiled_forward(model, window, overlap=256, scale=scale)
            
            del window

            # replace the current frame in the window with the reconstructed frame
            #input_window[clip_size//2] = torch.nn.functional.interpolate(img_E, scale_factor=1/scale, mode='bicubic')
            # remove the oldest frame from the window
            input_window.pop(0)

            img_E = util.tensor2uint(img_E, args.depth)
            #torch.set_rng_state(rng_state)

            # ------------------------------------
            # save results
            # ------------------------------------
            if args.video:
                img_E = cv2.resize(img_E, (int(output_res.split(':')[0]), int(output_res.split(':')[1])), interpolation=cv2.INTER_CUBIC)

            if args.video:
                video_encoder.add_frame(img_E)
            elif os.path.isdir(E_path):
                util.imsave(img_E, os.path.join(E_path, f'{image_names.pop(0)}_{suffix}.png'))
            else:
                util.imsave(img_E, E_path)

            end.record()
            torch.cuda.synchronize()

            idx += 1
            time_taken = start.elapsed_time(end)
            total_time += time_taken
            time_remaining = ((total_time / (idx)) * (img_count - (idx+1)))/1000

            print(f'{idx}/{img_count}   fps: {1000/time_taken:.2f}  frame time: {time_taken:2f}ms   time remaining: {math.trunc(time_remaining/3600)}h{math.trunc((time_remaining/60)%60)}m{math.trunc(time_remaining%60)}s ', end='\r')
    except KeyboardInterrupt:
        print("\nCaught KeyboardInterrupt, ending gracefully")
    except Exception as e:
        print("\n" + str(e))
    finally:
        # Clean up code that should run regardless of how the script ends
        if video_encoder is not None:
            try:
                video_encoder.stop()
                # Add timeout to join to prevent hanging
                video_encoder.join(timeout=5)
                if idx > 0:
                    print(f"Saved video to {E_path}")
            except Exception as e:
                print(f"Error while closing video encoder: {e}")
            finally:
                # Force close the output container if still open
                if hasattr(video_encoder, 'output_container') and video_encoder.output_container:
                    try:
                        video_encoder.output_container.close()
                    except:
                        pass

        if video_input:
            try:
                video_encoder.stop()
                # Add timeout to join to prevent hanging
                video_encoder.join(timeout=5)
                if idx > 0:
                    print(f"Saved video to {E_path}")
                    # Print hyperlink to output directory instead of file
                    output_dir = os.path.dirname(os.path.abspath(E_path))
                    print(f"\033]8;;file://{output_dir}\033\\Click to open output directory\033]8;;\033\\")
            except Exception as e:
                print(f"Error while closing video encoder: {e}")

        if idx > 0:
            print(f'Processed {idx} images in {timedelta(milliseconds=total_time)}, average {total_time / idx:.2f}ms per image              ')

        # Force exit to ensure all threads are terminated
        os._exit(0)

if __name__ == '__main__':

    main()
