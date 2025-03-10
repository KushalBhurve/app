import os
import time
import argparse
from mmgp import offload, safetensors2, profile_type 
try:
    import triton
except ImportError:
    pass
from pathlib import Path
from datetime import datetime
import gradio as gr
import random
import json
import wan
from wan.configs import MAX_AREA_CONFIGS, WAN_CONFIGS, SUPPORTED_SIZES
from wan.utils.utils import cache_video
from wan.modules.attention import get_attention_modes
import torch
import gc
import traceback
import math
import asyncio
from PIL import Image

lock_ui_attention = False
lock_ui_transformer = False
lock_ui_compile = False

def _parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a video from a text prompt or image using Gradio")

    parser.add_argument(
        "--quantize-transformer",
        action="store_true",
        help="On the fly 'transformer' quantization"
    )

    parser.add_argument(
        "--share",
        action="store_true",
        help="Create a shared URL to access webserver remotely"
    )

    parser.add_argument(
        "--lock-config",
        action="store_true",
        help="Prevent modifying the configuration from the web interface"
    )

    parser.add_argument(
        "--preload",
        type=str,
        default="0",
        help="Megabytes of the diffusion model to preload in VRAM"
    )

    parser.add_argument(
        "--multiple-images",
        action="store_true",
        help="Allow inputting multiple images with image to video"
    )


    parser.add_argument(
        "--lora-dir-i2v",
        type=str,
        default="loras_i2v",
        help="Path to a directory that contains Loras for i2v"
    )


    parser.add_argument(
        "--lora-dir",
        type=str,
        default="loras", 
        help="Path to a directory that contains Loras"
    )


    parser.add_argument(
        "--lora-preset",
        type=str,
        default="",
        help="Lora preset to preload"
    )

    parser.add_argument(
        "--lora-preset-i2v",
        type=str,
        default="",
        help="Lora preset to preload for i2v"
    )

    parser.add_argument(
        "--profile",
        type=str,
        default=-1,
        help="Profile No"
    )

    parser.add_argument(
        "--verbose",
        type=str,
        default=1,
        help="Verbose level"
    )

    parser.add_argument(
        "--server-port",
        type=str,
        default=0,
        help="Server port"
    )

    parser.add_argument(
        "--server-name",
        type=str,
        default="",
        help="Server name"
    )

    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="open browser"
    )

    parser.add_argument(
        "--t2v",
        action="store_true",
        help="text to video mode"
    )

    parser.add_argument(
        "--i2v",
        action="store_true",
        help="image to video mode"
    )

    parser.add_argument(
        "--compile",
        action="store_true",
        help="Enable pytorch compilation"
    )

    # parser.add_argument(
    #     "--fast",
    #     action="store_true",
    #     help="use Fast model"
    # )

    # parser.add_argument(
    #     "--fastest",
    #     action="store_true",
    #     help="activate the best config"
    # )

    parser.add_argument(
    "--attention",
    type=str,
    default="",
    help="attention mode"
    )

    parser.add_argument(
    "--vae-config",
    type=str,
    default="",
    help="vae config mode"
    )    


    args = parser.parse_args()

    return args

args = _parse_args()

preload =0
force_profile_no = -1
verbose_level = 1
quantizeTransformer = args.quantize_transformer

transformer_choices_t2v=["ckpts/wan2.1_text2video_1.3B_bf16.safetensors", "ckpts/wan2.1_text2video_14B_bf16.safetensors", "ckpts/wan2.1_text2video_14B_quanto_int8.safetensors"]   
transformer_choices_i2v=["ckpts/wan2.1_image2video_480p_14B_bf16.safetensors", "ckpts/wan2.1_image2video_480p_14B_quanto_int8.safetensors", "ckpts/wan2.1_image2video_720p_14B_bf16.safetensors", "ckpts/wan2.1_image2video_720p_14B_quanto_int8.safetensors"]
text_encoder_choices = ["ckpts/models_t5_umt5-xxl-enc-bf16.safetensors", "ckpts/models_t5_umt5-xxl-enc-quanto_int8.safetensors"]

server_config_filename = "gradio_config.json"

if not Path(server_config_filename).is_file():
    server_config = {"attention_mode" : "auto",  
                     "transformer_filename": transformer_choices_t2v[0], 
                     "transformer_filename_i2v": transformer_choices_i2v[1],  ########
                     "text_encoder_filename" : text_encoder_choices[1],
                     "compile" : "",
                     "default_ui": "t2v",
                     "vae_config": 0,
                     "profile" : profile_type.LowRAM_LowVRAM }

    with open(server_config_filename, "w", encoding="utf-8") as writer:
        writer.write(json.dumps(server_config))
else:
    with open(server_config_filename, "r", encoding="utf-8") as reader:
        text = reader.read()
    server_config = json.loads(text)

transformer_filename_t2v = server_config["transformer_filename"]
transformer_filename_i2v = server_config.get("transformer_filename_i2v", transformer_choices_i2v[1]) ########

text_encoder_filename = server_config["text_encoder_filename"]
attention_mode = server_config["attention_mode"]

if len(args.attention)> 0:
    if args.attention in ["auto", "sdpa", "sage", "sage2", "flash", "xformers"]:
        attention_mode = args.attention
        lock_ui_attention = True
    else:
        raise Exception(f"Unknown attention mode '{args.attention}'")

profile =  force_profile_no if force_profile_no >=0 else server_config["profile"]
compile = server_config.get("compile", "")
vae_config = server_config.get("vae_config", 0)
if len(args.vae_config) > 0:
    vae_config = int(args.vae_config)

default_ui = server_config.get("default_ui", "t2v") 
use_image2video = default_ui != "t2v"

use_image2video = True
lora_dir =args.lora_dir_i2v
lora_preselected_preset = args.lora_preset_i2v

default_tea_cache = 0

if  args.compile: #args.fastest or
    compile="transformer"
    lock_ui_compile = True


def download_models(transformer_filename, text_encoder_filename):
    def computeList(filename):
        pos = filename.rfind("/")
        filename = filename[pos+1:]
        return [filename]        
    
    from huggingface_hub import hf_hub_download, snapshot_download    
    repoId = "DeepBeepMeep/Wan2.1" 
    sourceFolderList = ["xlm-roberta-large", "",  ]
    fileList = [ [], ["Wan2.1_VAE.pth", "models_clip_open-clip-xlm-roberta-large-vit-huge-14.pth" ] + computeList(text_encoder_filename) + computeList(transformer_filename) ]   
    targetRoot = "ckpts/" 
    for sourceFolder, files in zip(sourceFolderList,fileList ):
        if len(files)==0:
            if not Path(targetRoot + sourceFolder).exists():
                snapshot_download(repo_id=repoId,  allow_patterns=sourceFolder +"/*", local_dir= targetRoot)
        else:
             for onefile in files:     
                if len(sourceFolder) > 0: 
                    if not os.path.isfile(targetRoot + sourceFolder + "/" + onefile ):          
                        hf_hub_download(repo_id=repoId,  filename=onefile, local_dir = targetRoot, subfolder=sourceFolder)
                else:
                    if not os.path.isfile(targetRoot + onefile ):          
                        hf_hub_download(repo_id=repoId,  filename=onefile, local_dir = targetRoot)


offload.default_verboseLevel = verbose_level

download_models(transformer_filename_i2v, text_encoder_filename) 

def sanitize_file_name(file_name):
    return file_name.replace("/","").replace("\\","").replace(":","").replace("|","").replace("?","").replace("<","").replace(">","").replace("\"","") 

def extract_preset(lset_name, loras):
    lset_name = sanitize_file_name(lset_name)
    if not lset_name.endswith(".lset"):
        lset_name_filename = os.path.join(lora_dir, lset_name + ".lset" ) 
    else:
        lset_name_filename = os.path.join(lora_dir, lset_name ) 

    if not os.path.isfile(lset_name_filename):
        raise gr.Error(f"Preset '{lset_name}' not found ")

    with open(lset_name_filename, "r", encoding="utf-8") as reader:
        text = reader.read()
    lset = json.loads(text)

    loras_choices_files = lset["loras"]
    loras_choices = []
    missing_loras = []
    for lora_file in loras_choices_files:
        loras_choice_no = loras.index(os.path.join(lora_dir, lora_file))
        if loras_choice_no < 0:
            missing_loras.append(lora_file)
        else:
            loras_choices.append(str(loras_choice_no))

    if len(missing_loras) > 0:
        raise gr.Error(f"Unable to apply Lora preset '{lset_name} because the following Loras files are missing: {missing_loras}")
    
    loras_mult_choices = lset["loras_mult"]
    return loras_choices, loras_mult_choices

def setup_loras(pipe,  lora_dir, lora_preselected_preset, split_linear_modules_map = None):
    loras =[]
    loras_names = []
    default_loras_choices = []
    default_loras_multis_str = ""
    loras_presets = []

    from pathlib import Path

    if lora_dir != None :
        if not os.path.isdir(lora_dir):
            raise Exception("--lora-dir should be a path to a directory that contains Loras")

    default_lora_preset = ""

    if lora_dir != None:
        import glob
        dir_loras =  glob.glob( os.path.join(lora_dir , "*.sft") ) + glob.glob( os.path.join(lora_dir , "*.safetensors") ) 
        dir_loras.sort()
        loras += [element for element in dir_loras if element not in loras ]

        dir_presets =  glob.glob( os.path.join(lora_dir , "*.lset") ) 
        dir_presets.sort()
        loras_presets = [ Path(Path(file_path).parts[-1]).stem for file_path in dir_presets]

    if len(loras) > 0:
        loras_names = [ Path(lora).stem for lora in loras  ]
        offload.load_loras_into_model(pipe["transformer"], loras,  activate_all_loras=False, split_linear_modules_map = split_linear_modules_map) #lora_multiplier,

    if len(lora_preselected_preset) > 0:
        if not os.path.isfile(os.path.join(lora_dir, lora_preselected_preset + ".lset")):
            raise Exception(f"Unknown preset '{lora_preselected_preset}'")
        default_lora_preset = lora_preselected_preset
        default_loras_choices, default_loras_multis_str= extract_preset(default_lora_preset, loras)

    return loras, loras_names, default_loras_choices, default_loras_multis_str, default_lora_preset, loras_presets

def get_auto_attention():
    for attn in ["sage2","sage","sdpa"]:
        if attn in attention_modes_supported:
            return attn
    return "sdpa"


def load_i2v_model(model_filename, value):


    print("load 14B-480P i2v model...")
    cfg = WAN_CONFIGS['i2v-14B']
    wan_model = wan.WanI2V(
        config=cfg,
        checkpoint_dir="ckpts",
        device_id=0,
        rank=0,
        t5_fsdp=False,
        dit_fsdp=False,
        use_usp=False,
        i2v720p= False,
        model_filename=model_filename,
        text_encoder_filename=text_encoder_filename

    )
    pipe = {"transformer": wan_model.model, "text_encoder" : wan_model.text_encoder.model,  "text_encoder_2": wan_model.clip.model, "vae": wan_model.vae.model } #

    return wan_model, pipe

def load_models(i2v,  lora_dir,  lora_preselected_preset ):
    download_models(transformer_filename_i2v if i2v else transformer_filename_t2v, text_encoder_filename) 

    wan_model, pipe = load_i2v_model(transformer_filename_i2v,"480P")

    kwargs = { "extraModelsToQuantize": None}
    if profile == 2 or profile == 4:
        kwargs["budgets"] = { "transformer" : 100 if preload  == 0 else preload, "text_encoder" : 100, "*" : 1000 }
    elif profile == 3:
        kwargs["budgets"] = { "*" : "70%" }


    loras, loras_names, default_loras_choices, default_loras_multis_str, default_lora_preset, loras_presets = setup_loras(pipe,  lora_dir, lora_preselected_preset, None)
    offloadobj = offload.profile(pipe, profile_no= profile, compile = compile, quantizeTransformer = quantizeTransformer, **kwargs)  


    return wan_model, offloadobj, loras, loras_names, default_loras_choices, default_loras_multis_str, default_lora_preset, loras_presets

wan_model, offloadobj,  loras, loras_names, default_loras_choices, default_loras_multis_str, default_lora_preset, loras_presets = load_models(use_image2video, lora_dir, lora_preselected_preset )
gen_in_progress = False

from moviepy.editor import ImageSequenceClip
import numpy as np

def save_video(final_frames, output_path, fps=24):
    assert final_frames.ndim == 4 and final_frames.shape[3] == 3, f"invalid shape: {final_frames} (need t h w c)"
    if final_frames.dtype != np.uint8:
        final_frames = (final_frames * 255).astype(np.uint8)
    ImageSequenceClip(list(final_frames), fps=fps).write_videofile(output_path, verbose= False, logger = None)

def build_callback(state, pipe, progress, status, num_inference_steps):
    def callback(step_idx, latents):
        step_idx += 1         
        if state.get("abort", False):
            # pipe._interrupt = True
            status_msg = status + " - Aborting"    
        elif step_idx  == num_inference_steps:
            status_msg = status + " - VAE Decoding"    
        else:
            status_msg = status + " - Denoising"   

        progress( (step_idx , num_inference_steps) , status_msg  ,  num_inference_steps)
            
    return callback

def expand_slist(slist, num_inference_steps ):
    new_slist= []
    inc =  len(slist) / num_inference_steps 
    pos = 0
    for i in range(num_inference_steps):
        new_slist.append(slist[ int(pos)])
        pos += inc
    return new_slist

def generate_video(
    prompt,
    negative_prompt,    
    resolution,
    video_length,
    seed,
    num_inference_steps,
    guidance_scale,
    flow_shift,
    embedded_guidance_scale,
    repeat_generation,
    tea_cache,
    tea_cache_start_step_perc,    
    loras_choices,
    loras_mult_choices,
    image_to_continue,
    video_to_continue,
    max_frames,
    RIFLEx_setting,
    state

):
    
    from PIL import Image
    import numpy as np
    import tempfile


    if wan_model == None:
        print("Unable to generate a Video while a new configuration is being applied.")
    if attention_mode == "auto":
        attn = get_auto_attention()
    elif attention_mode in attention_modes_supported:
        attn = attention_mode
    else:
        print(f"You have selected attention mode '{attention_mode}'. However it is not installed on your system. You should either install it or switch to the default 'sdpa' attention.")

    width, height = resolution.split("x")
    width, height = int(width), int(height)


    if use_image2video:
        if "480p" in  transformer_filename_i2v and width * height > 848*480:
            print("You must use the 720P image to video model to generate videos with a resolution equivalent to 720P")

        resolution = str(width) + "*" + str(height)  
        if  resolution not in ['720*1280', '1280*720', '480*832', '832*480']:
            print(f"Resolution {resolution} not supported by image 2 video")


    else:
        if "1.3B" in  transformer_filename_t2v and width * height > 848*480:
            print("You must use the 14B text to video model to generate videos with a resolution equivalent to 720P")

    
    offload.shared_state["_attention"] =  attn
 
     # VAE Tiling
    device_mem_capacity = torch.cuda.get_device_properties(0).total_memory / 1048576
    if vae_config == 0:
        if device_mem_capacity >= 24000:
            use_vae_config = 1            
        elif device_mem_capacity >= 8000:
            use_vae_config = 2
        else:          
            use_vae_config = 3
    else:
        use_vae_config = vae_config

    if use_vae_config == 1:
        VAE_tile_size = 0  
    elif use_vae_config == 2:
        VAE_tile_size = 256  
    else: 
        VAE_tile_size = 128  


    global gen_in_progress
    gen_in_progress = True
    temp_filename = None
    if len(prompt) ==0:
        return
    prompts = prompt.replace("\r", "").split("\n")

    if use_image2video:
        if image_to_continue is not None:
            if isinstance(image_to_continue, list):
                image_to_continue = [ tup[0] for tup in image_to_continue ]
            else:
                image_to_continue = [image_to_continue]
            if len(prompts) >= len(image_to_continue):
                if len(prompts) % len(image_to_continue) !=0:
                    print("If there are more text prompts than input images the number of text prompts should be dividable by the number of images")
                rep = len(prompts) // len(image_to_continue)
                new_image_to_continue = []
                for i, _ in enumerate(prompts):
                    new_image_to_continue.append(image_to_continue[i//rep] )
                image_to_continue = new_image_to_continue 
            else: 
                if len(image_to_continue) % len(prompts)  !=0:
                    print("If there are more input images than text prompts the number of images should be dividable by the number of text prompts")
                rep = len(image_to_continue) // len(prompts)  
                new_prompts = []
                for i, _ in enumerate(image_to_continue):
                    new_prompts.append(  prompts[ i//rep] )
                prompts = new_prompts

        elif video_to_continue != None and len(video_to_continue) >0 :
            input_image_or_video_path = video_to_continue
            # pipeline.num_input_frames = max_frames
            # pipeline.max_frames = max_frames
        else:
            return
    else:
        input_image_or_video_path = None


    if len(loras) > 0:
        def is_float(element: any) -> bool:
            if element is None: 
                return False
            try:
                float(element)
                return True
            except ValueError:
                return False
        list_mult_choices_nums = []
        if len(loras_mult_choices) > 0:
            list_mult_choices_str = loras_mult_choices.split(" ")
            for i, mult in enumerate(list_mult_choices_str):
                mult = mult.strip()
                if "," in mult:
                    multlist = mult.split(",")
                    slist = []
                    for smult in multlist:
                        if not is_float(smult):                
                            print(f"Lora sub value no {i+1} ({smult}) in Multiplier definition '{multlist}' is invalid")
                        slist.append(float(smult))
                    slist = expand_slist(slist, num_inference_steps )
                    list_mult_choices_nums.append(slist)
                else:
                    if not is_float(mult):                
                        print(f"Lora Multiplier no {i+1} ({mult}) is invalid")
                    list_mult_choices_nums.append(float(mult))
        if len(list_mult_choices_nums ) < len(loras_choices):
            list_mult_choices_nums  += [1.0] * ( len(loras_choices) - len(list_mult_choices_nums ) )

        offload.activate_loras(wan_model.model, loras_choices, list_mult_choices_nums)

    seed = None if seed == -1 else seed
    # negative_prompt = "" # not applicable in the inference
 
    enable_RIFLEx = RIFLEx_setting == 0 and video_length > (6* 16) or RIFLEx_setting == 1
    # VAE Tiling
    device_mem_capacity = torch.cuda.get_device_properties(0).total_memory / 1048576


   # TeaCache   
    trans = wan_model.model
    trans.enable_teacache = tea_cache > 0
 
    import random
    if seed == None or seed <0:
        seed = random.randint(0, 999999999)

    file_list = []
    state["file_list"] = file_list    
    from einops import rearrange
    save_path = os.path.join(os.getcwd(), "gradio_outputs")
    os.makedirs(save_path, exist_ok=True)
    video_no = 0
    total_video =  repeat_generation * len(prompts)
    abort = False
    start_time = time.time()
    for prompt in prompts:
        for _ in range(repeat_generation):
            if abort:
                break

            if trans.enable_teacache:
                trans.teacache_counter = 0
                trans.rel_l1_thresh = tea_cache
                trans.teacache_start_step =  max(math.ceil(tea_cache_start_step_perc*num_inference_steps/100),2)
                trans.previous_residual_uncond = None
                trans.previous_modulated_input_uncond = None                
                trans.previous_residual_cond = None
                trans.previous_modulated_input_cond= None                

                trans.teacache_cache_device = "cuda" if profile==3 or profile==1 else "cpu"                                 

            video_no += 1
            status = f"Video {video_no}/{total_video}"
            progress(0, desc=status + " - Encoding Prompt" )   
            
            callback = build_callback(state, trans, progress, status, num_inference_steps)


            gc.collect()
            torch.cuda.empty_cache()
            wan_model._interrupt = False
            try:
                if use_image2video:
                    samples = wan_model.generate(
                        prompt,
                        image_to_continue[ (video_no-1) % len(image_to_continue)],  
                        frame_num=(video_length // 4)* 4 + 1,
                        max_area=MAX_AREA_CONFIGS[resolution], 
                        shift=flow_shift,
                        sampling_steps=num_inference_steps,
                        guide_scale=guidance_scale,
                        n_prompt=negative_prompt,
                        seed=seed,
                        offload_model=False,
                        callback=callback,
                        enable_RIFLEx = enable_RIFLEx,
                        VAE_tile_size = VAE_tile_size
                    )

                else:
                    samples = wan_model.generate(
                        prompt,
                        frame_num=(video_length // 4)* 4 + 1,
                        size=(width, height),
                        shift=flow_shift,
                        sampling_steps=num_inference_steps,
                        guide_scale=guidance_scale,
                        n_prompt=negative_prompt,
                        seed=seed,
                        offload_model=False,
                        callback=callback,
                        enable_RIFLEx = enable_RIFLEx,
                        VAE_tile_size = VAE_tile_size
                    )
            except Exception as e:
                gen_in_progress = False
                if temp_filename!= None and  os.path.isfile(temp_filename):
                    os.remove(temp_filename)
                offload.last_offload_obj.unload_all()
                # if compile:
                #     cache_size = torch._dynamo.config.cache_size_limit                                      
                #     torch.compiler.reset()
                #     torch._dynamo.config.cache_size_limit = cache_size

                gc.collect()
                torch.cuda.empty_cache()
                s = str(e)
                keyword_list = ["vram", "VRAM", "memory", "triton", "cuda", "allocat"]
                VRAM_crash= False
                if any( keyword in s for keyword in keyword_list):
                    VRAM_crash = True
                else:
                    stack = traceback.extract_stack(f=None, limit=5)
                    for frame in stack:
                        if any( keyword in frame.name for keyword in keyword_list):
                            VRAM_crash = True
                            break
                if VRAM_crash:
                    print("The generation of the video has encountered an error: it is likely that you have unsufficient VRAM and you should therefore reduce the video resolution or its number of frames.")
                else:
                    print(f"The generation of the video has encountered an error, please check your terminal for more information. '{s}'")

            if trans.enable_teacache:
                trans.previous_residual_uncond = None
                trans.previous_residual_cond = None

            if samples != None:
                samples = samples.to("cpu")
            offload.last_offload_obj.unload_all()
            gc.collect()
            torch.cuda.empty_cache()

            if samples == None:
                end_time = time.time()
                abort = True
                yield f"Video generation was aborted. Total Generation Time: {end_time-start_time:.1f}s"
            else:
                sample = samples.cpu()
                # video = rearrange(sample.cpu().numpy(), "c t h w -> t h w c")

                time_flag = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d-%Hh%Mm%Ss")
                if os.name == 'nt':
                    file_name = f"{time_flag}_seed{seed}_{prompt[:50].replace('/','').strip()}.mp4".replace(':',' ').replace('\\',' ')
                else:
                    file_name = f"{time_flag}_seed{seed}_{prompt[:100].replace('/','').strip()}.mp4".replace(':',' ').replace('\\',' ')
                video_path = os.path.join(os.getcwd(), "gradio_outputs", file_name)        
                cache_video(
                    tensor=sample[None],
                    save_file=video_path,
                    fps=16,
                    nrow=1,
                    normalize=True,
                    value_range=(-1, 1))

                print(f"New video saved to Path: "+video_path)
                file_list.append(video_path)
                if video_no < total_video:
                    yield  status
                else:
                    end_time = time.time()
                    yield f"Total Generation Time: {end_time-start_time:.1f}s"
            seed += 1
  
    if temp_filename!= None and  os.path.isfile(temp_filename):
        os.remove(temp_filename)
    gen_in_progress = False

new_preset_msg = "Enter a Name for a Lora Preset or Choose One Above"



prompt = "Make the cat remove the goggles and dance"
negative_prompt=""
resolution="832x480"
video_length = 81
seed = -1
num_inference_steps=30
guidance_scale=5.0
flow_shift=3.0
embedded_guidance_scale=6.0
repeat_generation=1.0
tea_cache=0
tea_cache_start_step_perc= 20
loras_choices=default_loras_choices
loras_mult_choices=default_loras_multis_str
image_path = "./examples/i2v_input.JPG"
image_to_continue = Image.open(image_path)
video_to_continue=None
max_frames=9
RIFLEx_setting=0
state = gr.State({})

try:
    generate_video(
    prompt,
    negative_prompt,    
    resolution,
    video_length,
    seed,
    num_inference_steps,
    guidance_scale,
    flow_shift,
    embedded_guidance_scale,
    repeat_generation,
    tea_cache,
    tea_cache_start_step_perc,    
    loras_choices,
    loras_mult_choices,
    image_to_continue,
    video_to_continue,
    max_frames,
    RIFLEx_setting,
    state,

    )
except Exception as e:
    print(f"An error occurred: {e}")
