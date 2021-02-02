#
# MIT License -- see LICENSE.rst for details
# Copyright (c) 2020-21 Matthew Lovell and contributors
#
# ----------------------------------------------------------------------------
#
# This file is a variant of kodi_panel that copies the Pillow image,
# via luma.lcd, to a framebuffer.
#
# The first version of this file made use of Pytorinox's
# framebuffer.py.  However, the 2.0.0 release of luma.core includes a
# new linux_framebuffer class.  Using it permits for fewer changes.
#
# After kodi_panel launches, the blinking cursor from the console may
# still be visible.  On RPI systems adding
#
#   vt.global_cursor_default=0
#
# to the end of /boot/cmdline.txt will turn off that cursor.
# Note that the cmdline.txt file must be just a single line of text.
#
# ----------------------------------------------------------------------------
#
from luma.core import device

import os
from time import sleep

# kodi_panel modules
import config
import kodi_panel_display

import sys
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from datetime import datetime, timedelta
from aenum import Enum, extend_enum
from functools import lru_cache
import copy
import time
import logging
import requests
import json
import io
import re
import os
import threading
import warnings

# -------------------------------custom_function-----------------------------
# channel number lookup
ch_N = {
    "1"      : "1.0",
    "2"      : "2.0",
    "3"      : "2.1",
    "4"      : "3.1",
    "5"      : "4.1",
    "6"      : "5.1",
    "7"      : "6.1",
    "8"      : "7.1",
    "10"     : "9.1",
}


def strcb_channelnumber(info, screen_mode, layout_name):
    if 'MusicPlayer.Channels' in info:
        if info['MusicPlayer.Channels'] in ch_N:
            return ch_N[info['MusicPlayer.Channels']]
        else:
            return info['MusicPlayer.Channels']
    if 'VideoPlayer.AudioChannels' in info:
        if info['VideoPlayer.AudioChannels'] in ch_N:
            return ch_N[info['VideoPlayer.AudioChannels']]
        else:
            return info['VideoPlayer.AudioChannels']
    return ""

kodi_panel_display.STRING_CB["channelnumber"] = strcb_channelnumber

def strcb_vcodec(info, screen_mode, layout_name):
    if 'VideoPlayer.VideoCodec' in info:
        if info['VideoPlayer.VideoCodec'] in kodi_panel_display.codec_name.keys():
            return kodi_panel_display.codec_name[info['VideoPlayer.VideoCodec']]
        else:
            return info['VideoPlayer.VideoCodec']
    return ""

kodi_panel_display.STRING_CB["vcodec"] = strcb_vcodec

def strcb_timeshiftcheck(info, screen_mode, layout_name):
    if 'PVR.TimeShiftOffset' in info:
        if info['PVR.TimeShiftOffset'].startswith("00:0"):
            return "true"
    return ""

kodi_panel_display.STRING_CB["timeshiftcheck"] = strcb_timeshiftcheck



#------------Custom progress_bar---------------------
def calc_progress_custom(time_str, duration_str, layout_name):
    if (time_str == "" or duration_str == ""):
        return -1
    if not (1 <= time_str.count(":") <= 2 and
            1 <= duration_str.count(":") <= 2):
        return -1

    cur_secs = sum(
        int(x) * 60 ** i for i,
        x in enumerate(
            reversed(
                time_str.split(':'))))
    total_secs = sum(
        int(x) * 60 ** i for i,
        x in enumerate(
            reversed(
                duration_str.split(':'))))
    if layout_name == "V_PVR":
        cur_secs -= 600 #remove pvr start offset
        total_secs -= 1200 #remove pvr start and stop offsets


    # If either cur_secs or total_secs is negative, we fall through
    # and return -1, hiding the progress bar.  We do explicitly cap
    # the maximum progress that is possible at 1.

    if (cur_secs >= 0 and total_secs > 0):
        if (cur_secs >= total_secs):
            return 1
        else:
            return cur_secs / total_secs
    else:
        return -1

kodi_panel_display.calc_progress  = calc_progress_custom

#----------------- codec images-------------------------
codec_logo = {
    "ac3": "images/codeclogo/audio/ac3.png",
    "eac3": "images/codeclogo/audio/eac3.png",
    "dtshd_ma": "images/codeclogo/audio/dtshd_ma.png",
    "dtshd_hra": "images/codeclogo/audio/dtshd_hra.png",
    "dts": "images/codeclogo/audio/dts.png",
    "truehd": "images/codeclogo/audio/truehd.png",
    "wmapro": "images/codeclogo/audio/wmapro.png",
    "mp3float": "images/codeclogo/audio/mp3float.png",
    "flac": "images/codeclogo/audio/flac.png",
    "alac": "images/codeclogo/audio/alac.png",
    "vorbis": "images/codeclogo/audio/vorbis.png",
    "aac": "images/codeclogo/audio/aac.png",
    "pcm_s16be": "images/codeclogo/audio/pcm.png",
    "pcm_u8": "images/codeclogo/audio/pcm.png",
    "h264": "images/codeclogo/video/h264.png",
    "wvc1": "images/codeclogo/video/wvc1.png",
    "vc1": "images/codeclogo/video/wvc1.png",
}

def element_acodeclogo(image, draw, info, field, screen_mode, layout_name):
    if 'VideoPlayer.AudioCodec' in info:
        if info['VideoPlayer.AudioCodec'] in codec_logo:
            logoimg = Image.open(codec_logo[info['VideoPlayer.AudioCodec']])
            image.paste(logoimg, (field["posx"], field["posy"]))
            return ""
        else:
            return ""
    if 'MusicPlayer.Codec' in info:
        if info['MusicPlayer.Codec'] in codec_logo:
            logoimg = Image.open(codec_logo[info['MusicPlayer.Codec']])
            image.paste(logoimg, (field["posx"], field["posy"]))
            return ""
        else:
            return ""
    return ""

kodi_panel_display.ELEMENT_CB["acodeclogo"] = element_acodeclogo

def element_vcodeclogo(image, draw, info, field, screen_mode, layout_name):
    if 'VideoPlayer.VideoCodec' in info:
        if info['VideoPlayer.VideoCodec'] in codec_logo:
            logoimg = Image.open(codec_logo[info['VideoPlayer.VideoCodec']])
            image.paste(logoimg, (field["posx"], field["posy"]))
            return ""
        else:
            return ""
    return ""

kodi_panel_display.ELEMENT_CB["vcodeclogo"] = element_vcodeclogo
#--------live TV progress bat---------------------------
def element_livetv_progbar(image, draw, info, field, screen_mode, layout_name):
        # Calculate progress in media, using InfoLabels appropriate for LiveTV
    prog = kodi_panel_display.calc_progress(
        info["PVR.EpgEventElapsedTime"],
        info["PVR.EpgEventDuration"],
        kodi_panel_display.video_dmode.name
    )

    show_prog = True

       # If the field has a display conditional
       # defined, let's test that to decide if we should proceed.
    if ("display_if" in field or
           "display_ifnot" in field):
           show_prog = kodi_panel_display.check_display_expr(field,
                                          info,
                                          kodi_panel_display.ScreenMode.AUDIO,
                                          kodi_panel_display.video_dmode.name)

    if show_prog:
            kodi_panel_display.progress_bar(
               draw, field, prog,
               use_long_len = (info['PVR.EpgEventDuration'].count(":") == 2)
           )

kodi_panel_display.ELEMENT_CB["livetv_prog"] = element_livetv_progbar
#-----------------------------------------------------------------------------

# Use a Linux framebuffer via luma.core.device
device = device.linux_framebuffer("/dev/fb0",bgr=1)

# Don't try to use luma.lcd's backlight control ...
kodi_panel_display.USE_BACKLIGHT = False

# ... instead, lets make use of the sysfs interface for hardware PWM.
# The current form of this code assumes that one has loaded an
# RPi overlay such as pwm_2chan and that the backlight is
# controlled via GPIO18 / PWM0.
#
# This is a (hopefully) temporary form for this code.

screen_state = 0

def screen_on_pwm():
    global screen_state
    if screen_state == 0:
        result = os.system("echo 1 > /sys/class/pwm/pwmchip0/pwm0/enable")
        screen_state = 1

def screen_off_pwm():
    global screen_state
    if screen_state == 1:
        result = os.system("echo 0 > /sys/class/pwm/pwmchip0/pwm0/enable")
        screen_state = 0


if __name__ == "__main__":
    # Setup PWM
    if config.settings["USE_HW_PWM"]:
        os.system("echo 0 > /sys/class/pwm/pwmchip0/export")
        sleep(0.150)
        freq_cmd   = "echo " + str(config.settings["HW_PWM_FREQ"]) + " > /sys/class/pwm/pwmchip0/pwm0/period"
        period_cmd = "echo " + str(int(config.settings["HW_PWM_FREQ"] *
                                       config.settings["HW_PWM_LEVEL"])) + " > /sys/class/pwm/pwmchip0/pwm0/duty_cycle"
        os.system(freq_cmd)
        os.system(period_cmd)
        screen_on_pwm()
        kodi_panel_display.screen_on  = screen_on_pwm
        kodi_panel_display.screen_off = screen_off_pwm

    try:
        kodi_panel_display.main(device)
    except KeyboardInterrupt:
        kodi_panel_display.shutdown()
        screen_on_pwm()
        if config.settings["USE_HW_PWM"]:
            os.system("echo 0 > /sys/class/pwm/pwmchip0/unexport")

        pass
