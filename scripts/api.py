'''
Author: SpenserCai
Date: 2023-08-20 17:28:26
version: 
LastEditors: SpenserCai
LastEditTime: 2023-08-21 17:05:30
Description: file content
'''
from fastapi import FastAPI, Body

from modules.api.models import *
from modules import scripts, shared
from modules.api import api
from modules import paths_internal
import gradio as gr
from PIL import Image
import urllib.request
from urllib.parse import urlparse
from modules import script_callbacks

import torch, uuid
import os, sys, shutil
import base64
from src.utils.preprocess import CropAndExtract
from src.test_audio2coeff import Audio2Coeff  
from src.facerender.animate import AnimateFromCoeff
from src.generate_batch import get_data
from src.generate_facerender_batch import get_facerender_data

from src.utils.init_path import init_path

from pydub import AudioSegment


def sadtalker_api(_: gr.Blocks, app: FastAPI):
    @app.post("/sadtalker/generate")
    async def sadtalker_generate(
        source_image: str = Body("",title="source image url"),
        source_audio: str = Body("",title="audio url"),
        preprocess: str = Body("full",title="How to handle input image"),
        still_mode: bool = Body(True,title="fewer head motion, works with preprocess `full`"),
        use_enhancer: bool = Body(True,title="GFPGAN as Face enhancer"),
    ):
        st = SadTalker()
        result = st.test(source_image=source_image,driven_audio=source_audio,preprocess=preprocess,still_mode=still_mode,use_enhancer=use_enhancer)
        return {'result':result}


def mp3_to_wav(mp3_filename,wav_filename,frame_rate):
    mp3_file = AudioSegment.from_file(file=mp3_filename)
    mp3_file.set_frame_rate(frame_rate).export(wav_filename,format="wav")


class SadTalker():

    def __init__(self, checkpoint_path='./checkpoints', config_path='./src/config', lazy_load=False):

        if torch.cuda.is_available() :
            device = "cuda"
        else:
            device = "cpu"
        
        self.device = device

        os.environ['TORCH_HOME']= checkpoint_path

        self.checkpoint_path = checkpoint_path
        self.config_path = config_path
      

    def test(self, source_image, driven_audio, preprocess='crop', 
        still_mode=False,  use_enhancer=False, batch_size=1, size=256, 
        pose_style = 0, exp_scale=1.0, 
        use_ref_video = False,
        ref_video = None,
        ref_info = None,
        use_idle_mode = False,
        length_of_audio = 0, use_blink=True,
        result_dir='./results/'):

        self.sadtalker_paths = init_path(self.checkpoint_path, self.config_path, size, False, preprocess)
        print(self.sadtalker_paths)
            
        self.audio_to_coeff = Audio2Coeff(self.sadtalker_paths, self.device)
        self.preprocess_model = CropAndExtract(self.sadtalker_paths, self.device)
        self.animate_from_coeff = AnimateFromCoeff(self.sadtalker_paths, self.device)

        time_tag = str(uuid.uuid4())
        save_dir = os.path.join(result_dir, time_tag)
        os.makedirs(save_dir, exist_ok=True)

        input_dir = os.path.join(save_dir, 'input')
        os.makedirs(input_dir, exist_ok=True)

        # urllib.request.urlretrieve(source_image, input_dir)
        source_image_name = os.path.basename(urlparse(source_image).path)
        pic_path=os.path.join(input_dir, source_image_name)
        urllib.request.urlretrieve(source_image, pic_path)
        source_audio_name = os.path.basename(urlparse(driven_audio).path)
        audio_path=os.path.join(input_dir, source_audio_name)
        urllib.request.urlretrieve(driven_audio, audio_path)   

        os.makedirs(save_dir, exist_ok=True)
        
        #crop image and extract 3dmm from image
        first_frame_dir = os.path.join(save_dir, 'first_frame_dir')
        os.makedirs(first_frame_dir, exist_ok=True)
        first_coeff_path, crop_pic_path, crop_info = self.preprocess_model.generate(pic_path, first_frame_dir, preprocess, True, size)
        
        if first_coeff_path is None:
            raise AttributeError("No face is detected")

        if use_ref_video:
            print('using ref video for genreation')
            ref_video_videoname = os.path.splitext(os.path.split(ref_video)[-1])[0]
            ref_video_frame_dir = os.path.join(save_dir, ref_video_videoname)
            os.makedirs(ref_video_frame_dir, exist_ok=True)
            print('3DMM Extraction for the reference video providing pose')
            ref_video_coeff_path, _, _ =  self.preprocess_model.generate(ref_video, ref_video_frame_dir, preprocess, source_image_flag=False)
        else:
            ref_video_coeff_path = None

        if use_ref_video:
            if ref_info == 'pose':
                ref_pose_coeff_path = ref_video_coeff_path
                ref_eyeblink_coeff_path = None
            elif ref_info == 'blink':
                ref_pose_coeff_path = None
                ref_eyeblink_coeff_path = ref_video_coeff_path
            elif ref_info == 'pose+blink':
                ref_pose_coeff_path = ref_video_coeff_path
                ref_eyeblink_coeff_path = ref_video_coeff_path
            elif ref_info == 'all':            
                ref_pose_coeff_path = None
                ref_eyeblink_coeff_path = None
            else:
                raise('error in refinfo')
        else:
            ref_pose_coeff_path = None
            ref_eyeblink_coeff_path = None

        #audio2ceoff
        if use_ref_video and ref_info == 'all':
            coeff_path = ref_video_coeff_path # self.audio_to_coeff.generate(batch, save_dir, pose_style, ref_pose_coeff_path)
        else:
            batch = get_data(first_coeff_path, audio_path, self.device, ref_eyeblink_coeff_path=ref_eyeblink_coeff_path, still=still_mode, idlemode=use_idle_mode, length_of_audio=length_of_audio, use_blink=use_blink) # longer audio?
            coeff_path = self.audio_to_coeff.generate(batch, save_dir, pose_style, ref_pose_coeff_path)

        #coeff2video
        data = get_facerender_data(coeff_path, crop_pic_path, first_coeff_path, audio_path, batch_size, still_mode=still_mode, preprocess=preprocess, size=size, expression_scale = exp_scale)
        return_path = self.animate_from_coeff.generate(data, save_dir,  pic_path, crop_info, enhancer='gfpgan' if use_enhancer else None, preprocess=preprocess, img_size=size)
        video_name = data['video_name']
        print(f'The generated video is named {video_name} in {save_dir}')

        del self.preprocess_model
        del self.audio_to_coeff
        del self.animate_from_coeff

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
            
        import gc; gc.collect()

        if return_path:
            with open(return_path, "rb") as videoFile:
                return base64.b64encode(videoFile.read())
        else:
            return ''
        
script_callbacks.on_app_started(sadtalker_api)