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
