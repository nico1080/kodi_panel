#
# MIT License
#
# Copyright (c) 2020  Matthew Lovell
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from luma.core.interface.serial import spi
from luma.core.render import canvas
from luma.lcd.device import ili9341

import signal
import sys
import RPi.GPIO as GPIO

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from datetime import datetime, timedelta
from enum import Enum
import time
import logging
import requests
import json
import io
import re
import os
import threading

# ----------------------------------------------------------------------------
PANEL_VER = "v0.71"

base_url = "http://localhost:8080"   # running on same box as Kodi
rpc_url  = base_url + "/jsonrpc"
headers  = {'content-type': 'application/json'}

# Image handling
frameSize       = (320, 240)
thumb_height    = 140
last_image_path = None
last_thumb      = None

# Thumbnail defaults (these don't get resized)
kodi_thumb      = "./images/kodi_thumb.jpg"
default_thumb   = "./images/music_icon.png"
default_airplay = "./images/airplay_thumb.png"

# RegEx for recognizing AirPlay images (compiled once)
special_re = re.compile(r'^special:\/\/temp\/(airtunes_album_thumb\.(png|jpg))')

# Track info fonts
font_main = ImageFont.truetype("fonts/FreeSans.ttf", 22, encoding='unic')
font_bold = ImageFont.truetype("fonts/FreeSansBold.ttf", 22, encoding='unic')
font_sm   = ImageFont.truetype("fonts/FreeSans.ttf", 18, encoding='unic')
font_tiny = ImageFont.truetype("fonts/FreeSans.ttf", 11, encoding='unic')

# 7-Segment Font for time and track number
font7S    = ImageFont.truetype("fonts/DSEG14Classic-Regular.ttf", 32)
font7S_sm = ImageFont.truetype("fonts/DSEG14Classic-Regular.ttf", 11)

# Colors
color7S       = 'SpringGreen'   # 7-Segment color
color_progbg  = 'dimgrey'       # progress bar background
color_progfg  = color7S         # progress bar foreground
color_artist  = 'yellow'        # artist name

# Pillow objects
image  = Image.new('RGB', (frameSize), 'black')
draw   = ImageDraw.Draw(image)

# Audio/Video codec lookup
codec_name = {
    "ac3"      : "DD",
    "eac3"     : "DD",
    "dtshd_ma" : "DTS-MA",
    "dca"      : "DTS",
    "truehd"   : "DD-HD",
    "wmapro"   : "WMA",
    "mp3float" : "MP3",
    "flac"     : "FLAC",
    "alac"     : "ALAC",
    "vorbis"   : "OggV",
    "aac"      : "AAC",
    "pcm_s16be": "PCM",
    "mp2"      : "MP2",
    "pcm_u8"   : "PCM",
    "BXA"      : "AirPlay",    # used with AirPlay
    "dsd_lsbf_planar": "DSD",
}


# Audio info display mode.  The next() function serves to switch modes in
# response to screen touches.  The list is intended to grow, as other
# ideas for layouts are proposed.
class ADisplay(Enum):
    DEFAULT    = 0   # small art, elapsed time, track info
    FULLSCREEN = 1   # fullscreen cover art
    FULL_PROG  = 2   # fullscreen art with vertical progress bar

    def next(self):
        cls = self.__class__
        members = list(cls)
        index = members.index(self) + 1
        if index >= len(members):
            index = 0
        return members[index]

# At startup, just use the default layout for audio info.  This
# setting, if serialized and stored someplace, could be made
# persistent across script invocations if desired.
audio_dmode = ADisplay.DEFAULT


# Screen layout details
LAYOUT = \
{ ADisplay.DEFAULT :
  {
    # Artwork position and size
    "thumb" : { "pos": (5, 5), "size": thumb_height },

    # Progress bar.  Two versions are possible, short and long,
    # depending upon the MusicPlayer.Time string.
    "prog"  : { "pos": (150, 7),
                "short_len": 104,  "long_len": 164,
                "height": 8 },

    # All other text fields, including any labels
    "fields" :
    [
        { "name": "MusicPlayer.Time",          "pos": (148, 20), "font": font7S, "fill":color7S },

        { "name":  "MusicPlayer.TrackNumber",  "pos": (148, 73),  "font": font7S,     "fill": color7S,
          "label": "Track",                   "lpos": (148, 60), "lfont": font_tiny, "lfill": "white" },

        { "name": "MusicPlayer.Duration", "pos": (230, 60), "font": font_tiny, "fill": "white" },
        { "name": "codec",                "pos": (230, 74), "font": font_tiny, "fill": "white" },
        { "name": "MusicPlayer.Genre",    "pos": (230, 88), "font": font_tiny, "fill": "white", "trunc":1 },
        { "name": "MusicPlayer.Year",     "pos": (230,102), "font": font_tiny, "fill": "white" },

        { "name": "MusicPlayer.Title",    "pos": (5, 152),  "font": font_main, "fill": "white",      "trunc":1 },
        { "name": "MusicPlayer.Album",    "pos": (5, 180),  "font": font_sm,   "fill": "white",      "trunc":1 },
        { "name": "artist",               "pos": (5, 205),  "font": font_sm,   "fill": color_artist, "trunc":1 },
    ]
  },

  ADisplay.FULLSCREEN :
  {
    # artwork size, position is determined by centering
    "thumb"   : { "size": frameSize[1]-5 },      
  },

  ADisplay.FULL_PROG :
  {
    # artwork size, position is determined by centering      
    "thumb" : { "size": frameSize[1]-5 },
      
    # vertical progress bar
    "prog" : { "pos": (frameSize[0]-12, 1),
               "len": 10,
               "height": frameSize[1]-4 },
  },  

}




# GPIO assignment for screen's touch interrupt (T_IRQ), using RPi.GPIO
# numbering.  Find a pin that's unused by luma.  The touchscreen chip
# in my display has its own internal pullup resistor, so below no
# pull-up is specified.
TOUCH_INT      = 19
USE_TOUCH      = True   # Set False to not use interrupt at all

# Internal state variables used to manage screen presses
kodi_active    = False
screen_press   = False
screen_on      = False
screen_wake    = 15    # status screen waketime, in seconds
screen_offtime = datetime.now()

# Provide a lock to ensure update_display() is single-threaded.  This
# is likely unnecessary given Python's GIL, but is certainly safe.
lock = threading.Lock()

# Finally, a handle to the ILI9341-driven SPI panel via luma.lcd.
#
# The backlight signal (with inline resistor NEEDED) is connected to
# GPIO18, physical pin 12.  Recall that the GPIOx number is using
# RPi.GPIO's scheme!
#
# As of Oct 2020, here's what luma.lcd's online documentation
# recommended, all of which is per RPi.GPIO pin naming:
#
#   LCD pin     |  RPi.GPIO name   |  Odroid C4 pin #
#   ------------|------------------|-----------------
#   VCC         |  3V3             |  1 or 17
#   GND         |  GND             |  9 or 25 or 39
#   CS          |  GPIO8           |  24
#   RST / RESET |  GPIO24          |  18
#   DC          |  GPIO23          |  16
#   MOSI        |  GPIO10 (MOSI)   |  19
#   SCLK / CLK  |  GPIO11 (SCLK)   |  23
#   LED         |  GPIO18          |  12
#   ------------|------------------|-----------------
#
# Originally, the constructor for ili9341 also included a
# framebuffer="full_frame" argument.  That proved unnecessary
# once non-zero reset hold and release times were specified
# for the device.
#
serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25,
             reset_hold_time=0.2, reset_release_time=0.2)
device = ili9341(serial, active_low=False, width=320, height=240,
                 bus_speed_hz=32000000
                 )

# ----------------------------------------------------------------------------

# Render text at the specified location, truncating characters and
# placing a final ellipsis if the string is too wide to display in its
# entirety.
def truncate_text(pil_draw, xy, text, fill, font):
    truncating = 0
    new_text = text
    t_width, t_height = pil_draw.textsize(new_text, font)
    while t_width > (frameSize[0] - 20):
        truncating = 1
        new_text = new_text[:-1]
        t_width, t_height = pil_draw.textsize(new_text, font)
    if truncating:
        new_text += "\u2026"
    pil_draw.text(xy, new_text, fill, font)


# Draw (by default) a horizontal progress bar at the specified
# location, filling from left to right.  A vertical bar can be drawn
# if specified, filling from bottom to top.
def progress_bar(pil_draw, bgcolor, color, x, y, w, h, progress, vertical=False):
    pil_draw.rectangle((x,y, x+w, y+h),fill=bgcolor)

    if progress <= 0:
        progress = 0.01
    if progress >1:
        progress = 1

    if vertical:
        dh = h*progress
        pil_draw.rectangle((x,y+h-dh,x+w,y+h),fill=color)
    else:
        dw = w*progress
        pil_draw.rectangle((x,y, x+dw, y+h),fill=color)


# Retrieve cover art or a default thumbnail.  Cover art gets resized
# to the provided thumb_size, but any default images are used as-is.
#
# Note that details of retrieval seem to differ depending upon whether
# Kodi is playing from its library, from UPnp/DLNA, or from Airplay.
#
# The global last_image_path is intended to let any given image file
# be fetched and resized just *once*.  Subsequent calls just reuse the
# same data, provided that the caller preserves and passes in
# prev_image.
#
# The info argument must be the result of an XBMC.GetInfoLabels
# JSON-RPC call to Kodi.
def get_artwork(info, prev_image, thumb_size):
    global last_image_path
    image_set     = False
    resize_needed = False

    cover = None   # retrieved artwork, original size
    thumb = None   # resized artwork

    if (info['MusicPlayer.Cover'] != '' and
        info['MusicPlayer.Cover'] != 'DefaultAlbumCover.png' and
        not special_re.match(info['MusicPlayer.Cover'])):

        image_path = info['MusicPlayer.Cover']
        #print("image_path : ", image_path) # debug info

        if (image_path == last_image_path and prev_image):
            # Fall through and just return prev_image
            image_set = True
        else:
            last_image_path = image_path
            if image_path.startswith("http://"):
                image_url = image_path
            else:
                payload = {
                    "jsonrpc": "2.0",
                    "method"  : "Files.PrepareDownload",
                    "params"  : {"path": image_path},
                    "id"      : 5,
                }
                response = requests.post(rpc_url, data=json.dumps(payload), headers=headers).json()
                #print("Response: ", json.dumps(response))  # debug info

                if ('details' in response['result'].keys() and
                    'path' in response['result']['details'].keys()) :
                    image_url = base_url + "/" + response['result']['details']['path']
                    #print("image_url : ", image_url) # debug info

            r = requests.get(image_url, stream = True)
            # check that the retrieval was successful before proceeding
            if r.status_code == 200:
                try:
                    r.raw.decode_content = True
                    cover = Image.open(io.BytesIO(r.content))
                    image_set     = True
                    resize_needed = True
                except:
                    cover = Image.open(default_thumb)
                    prev_image = cover
                    image_set     = True
                    resize_needed = False

    # finally, if we still don't have anything, check if is Airplay active
    if not image_set:
        resize_needed = False
        if special_re.match(info['MusicPlayer.Cover']):
            airplay_thumb = "/storage/.kodi/temp/" + special_re.match(info['MusicPlayer.Cover']).group(1)
            if os.path.isfile(airplay_thumb):
                last_image_path = airplay_thumb
                resize_needed   = True
            else:
                last_image_path = default_airplay
        else:
            # default image when no artwork is available
            last_image_path = default_thumb

        cover = Image.open(last_image_path)
        prev_image = cover
        image_set = True

    # is resizing needed?
    if (image_set and resize_needed):
        # resize while maintaining aspect ratio, if possible
        orig_w, orig_h = cover.size[0], cover.size[1]
        shrink    = (float(thumb_size)/orig_h)
        new_width = int(float(orig_h)*float(shrink))
        # just crop if the image turns out to be really wide
        if new_width > thumb_size:
            thumb = cover.resize((new_width, thumb_size), Image.ANTIALIAS).crop((0,0,140,thumb_size))
        else:
            thumb = cover.resize((new_width, thumb_size), Image.ANTIALIAS)
        prev_image = thumb

    if image_set:
        return prev_image
    else:
        return None


# Idle status screen (shown upon a screen press)
#
# First argument is a Pillow ImageDraw object.
# Second argument is a dictionary loaded from Kodi system status fields.
# This argument is the string to use for current state of the system
#
def status_screen(draw, kodi_status, summary_string):
    # Render screen
    kodi_icon = Image.open(kodi_thumb)
    image.paste(kodi_icon, (5, 5))
    draw.text(( 145, 8), "kodi_panel " + PANEL_VER, fill=color_artist, font=font_main)

    # pithy summary status
    draw.text(( 145, 35), summary_string,  fill='white', font=font_sm)

    # time in 7-segment font
    time_parts = kodi_status['System.Time'].split(" ")
    time_width, time_height = draw.textsize(time_parts[0], font7S)
    draw.text((145,73), time_parts[0], fill=color7S, font=font7S)
    draw.text((145 + time_width + 5, 73), time_parts[1], fill=color7S, font=font7S_sm)

    draw.text((5, 150), kodi_status['System.Date'], fill='white', font=font_sm)
    draw.text((5, 175), "Up: " + kodi_status['System.Uptime'], fill='white', font=font_sm)
    draw.text((5, 200), "CPU: " + kodi_status['System.CPUTemperature'], fill='white', font=font_sm)


# Audio info screens (shown when music is playing)
#
# For the moment, the two full-screen variants are short enough that all 3 modes
# are handled here in this function.
#
# First two arguments are Pillow Image and ImageDraw objects.
# Third argument is a dictionary loaded from Kodi with relevant track fields.
# Fourth argument is a float representing progress through the track.
def audio_screens(image, draw, info, prog):
    global audio_dmode
    global last_thumb
    global last_image_path

    # Default display -- all info with small artwork
    if audio_dmode == ADisplay.DEFAULT:
        layout = LAYOUT[audio_dmode]

        # retrieve cover image from Kodi, if it exists and needs a refresh
        last_thumb = get_artwork(info, last_thumb, layout["thumb"]["size"])
        if last_thumb:
            image.paste(last_thumb, layout["thumb"]["pos"])

        # progress bar
        if prog != -1:
            if info['MusicPlayer.Time'].count(":") == 2:
                # longer bar for longer displayed time
                progress_bar(draw, color_progbg, color_progfg,
                             layout["prog"]["pos"][0], layout["prog"]["pos"][1],
                             layout["prog"]["long_len"], layout["prog"]["height"],
                             prog)
            else:
                progress_bar(draw, color_progbg, color_progfg,
                             layout["prog"]["pos"][0], layout["prog"]["pos"][1],
                             layout["prog"]["short_len"], layout["prog"]["height"],
                             prog)

        # text fields
        txt_field = layout["fields"]
        for index in range(len(txt_field)):

            # special treatment for codec, which gets a lookup
            if txt_field[index]["name"] == "codec":
                if info['MusicPlayer.Codec'] in codec_name.keys():
                    draw.text(txt_field[index]["pos"],
                              codec_name[info['MusicPlayer.Codec']],
                              fill=txt_field[index]["fill"],
                              font=txt_field[index]["font"])

            # special treatment for MusicPlayer.Artist
            elif txt_field[index]["name"] == "artist":
                if info['MusicPlayer.Artist'] != "":
                    truncate_text(draw, txt_field[index]["pos"],
                                  info['MusicPlayer.Artist'],
                                  fill=txt_field[index]["fill"],
                                  font=txt_field[index]["font"])
                elif info['MusicPlayer.Property(Role.Composer)'] != "":
                    truncate_text(draw, txt_field[index]["pos"],
                                  "(" + info['MusicPlayer.Property(Role.Composer)'] + ")",
                                  fill=txt_field[index]["fill"],
                                  font=txt_field[index]["font"])

            # all other fields
            else:
                if (txt_field[index]["name"] in info.keys() and
                    info[txt_field[index]["name"]] != ""):
                    # ender any label first
                    if "label" in txt_field[index]:
                        draw.text(txt_field[index]["lpos"], txt_field[index]["label"],
                                  fill=txt_field[index]["lfill"], font=txt_field[index]["lfont"])
                    # now render the field itself
                    if "trunc" in txt_field[index].keys():
                        truncate_text(draw, txt_field[index]["pos"],
                                      info[txt_field[index]["name"]],
                                      fill=txt_field[index]["fill"],
                                      font=txt_field[index]["font"])
                    else:
                        draw.text(txt_field[index]["pos"],
                                  info[txt_field[index]["name"]],
                                  fill=txt_field[index]["fill"],
                                  font=txt_field[index]["font"])


    # Full-screen art
    elif audio_dmode == ADisplay.FULLSCREEN:
        layout = LAYOUT[audio_dmode]
        # retrieve cover image from Kodi, if it exists and needs a refresh
        last_thumb = get_artwork(info, last_thumb, layout["thumb"]["size"])
        if last_thumb:
            image.paste(last_thumb, (int((frameSize[0]-last_thumb.width)/2), int((frameSize[1]-last_thumb.height)/2)))


    # Full-screen art with progress bar
    elif audio_dmode == ADisplay.FULL_PROG:
        layout = LAYOUT[audio_dmode]
        # retrieve cover image from Kodi, if it exists and needs a refresh
        last_thumb = get_artwork(info, last_thumb, layout["thumb"]["size"])
        if last_thumb:
            image.paste(last_thumb, (int((frameSize[0]-last_thumb.width)/2), int((frameSize[1]-last_thumb.height)/2)))

        # vertical progress bar
        if prog != -1:
            progress_bar(draw, color_progbg, color_progfg,
                         layout["prog"]["pos"][0], layout["prog"]["pos"][1],
                         layout["prog"]["len"],
                         layout["prog"]["height"],
                         prog, vertical=True)


# Kodi-polling and image rendering function
#
# Locations and sizes (aside from font size) are all hard-coded in
# this function.  If anyone wanted to be ambitious and accommodate
# some form of programmable layout, you would start here.  Otherwise,
# just adjust to taste and desired outcome!
#
def update_display():
    global last_image_path
    global last_thumb
    global screen_press
    global screen_on
    global screen_offtime
    global audio_dmode

    lock.acquire()

    # Start with a blank slate
    draw.rectangle([(0,0), (frameSize[0],frameSize[1])], 'black', 'black')

    # Check if the screen_on time has expired
    if (screen_on and datetime.now() >= screen_offtime):
        screen_on = False
        device.backlight(False)

    # Ask Kodi whether anything is playing...
    payload = {
        "jsonrpc": "2.0",
        "method"  : "Player.GetActivePlayers",
        "id"      : 3,
    }
    response = requests.post(rpc_url, data=json.dumps(payload), headers=headers).json()

    if (len(response['result']) == 0 or
        response['result'][0]['type'] != 'audio'):
        # Nothing is playing or non-audio is playing, but check for screen
        # press before proceeding
        last_image_path = None
        last_thumb = None

        if screen_press:
            screen_press = False
            device.backlight(True)
            screen_on = True
            screen_offtime = datetime.now() + timedelta(seconds=screen_wake)

        if screen_on:
            # Idle status screen
            if len(response['result']) == 0:
                summary = "Idle"
            elif response['result'][0]['type'] == 'video':
                summary = "Video playing"
            elif response['result'][0]['type'] == 'picture':
                summary = "Photo viewing"

            payload = {
                "jsonrpc": "2.0",
                "method"  : "XBMC.GetInfoLabels",
                "params"  : {"labels": ["System.Uptime",
                                        "System.CPUTemperature",
                                        "System.Date",
                                        "System.Time",
                ]},
                "id"      : 10,
            }
            status_resp = requests.post(rpc_url, data=json.dumps(payload), headers=headers).json()
            status_screen(draw, status_resp['result'], summary)
        else:
            device.backlight(False)

    else:
        # Audio is playing!
        device.backlight(True)

        # Change display modes upon any screen press, forcing
        # a re-fetch of any artwork
        if screen_press:
            screen_press = False
            audio_dmode = audio_dmode.next()
            print(datetime.now(), "Touchscreen pressed -- audio display mode now", audio_dmode.name)
            last_image_path = None
            last_thumb = None

        # Retrieve (almost) all desired info in a single JSON-RPC call
        payload = {
            "jsonrpc": "2.0",
            "method"  : "XBMC.GetInfoLabels",
            "params"  : {"labels": ["MusicPlayer.Title",
                                    "MusicPlayer.Album",
                                    "MusicPlayer.Artist",
                                    "MusicPlayer.Time",
                                    "MusicPlayer.Duration",
                                    "MusicPlayer.TrackNumber",
                                    "MusicPlayer.Property(Role.Composer)",
                                    "MusicPlayer.Codec",
                                    "MusicPlayer.Year",
                                    "MusicPlayer.Genre",
                                    "MusicPlayer.Cover",
            ]},
            "id"      : 4,
        }
        response = requests.post(rpc_url, data=json.dumps(payload), headers=headers).json()
        #print("Response: ", json.dumps(response))
        track_info = response['result']

        # Progress information in Kodi Leia must be fetched separately.  This
        # looks to be fixed in Kodi Matrix.
        payload = {
            "jsonrpc": "2.0",
            "method"  : "Player.GetProperties",
            "params"  : {
                "playerid": 0,
                "properties" : ["percentage"],
            },
            "id"      : "prog",
        }
        prog_response = requests.post(rpc_url, data=json.dumps(payload), headers=headers).json()
        if 'percentage' in prog_response['result'].keys():
            prog = float(prog_response['result']['percentage']) / 100.0
        else:
            prog = -1

        # Audio info
        audio_screens(image, draw, track_info, prog)

    # Output to OLED/LCD display
    device.display(image)
    lock.release()


# Interrupt callback target from RPi.GPIO for T_IRQ
def touch_callback(channel):
    global screen_press
    global kodi_active
    screen_press = True
    #print(datetime.now(), "Touchscreen pressed")
    if kodi_active:
        try:
            update_display()
        except:
            pass


def main():
    global kodi_active
    global screen_press
    kodi_active = False

    print(datetime.now(), "Starting")
    # turn down verbosity from http connections
    logging.basicConfig()
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # setup T_IRQ as a GPIO interrupt, if enabled
    if USE_TOUCH:
        print(datetime.now(), "Setting up touchscreen interrupt")
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(TOUCH_INT, GPIO.IN)
        GPIO.add_event_detect(TOUCH_INT, GPIO.FALLING,
                              callback=touch_callback, bouncetime=950)

    # main communication loop
    while True:
        device.backlight(True)
        draw.rectangle([(0,0), (frameSize[0],frameSize[1])], 'black', 'black')
        draw.text(( 5, 5), "Waiting to connect with Kodi...",  fill='white', font=font_main)
        device.display(image)

        while True:
            # ensure Kodi is up and accessible
            payload = {
                "jsonrpc": "2.0",
                "method"  : "JSONRPC.Ping",
                "id"      : 2,
            }

            try:
                response = requests.post(rpc_url, data=json.dumps(payload), headers=headers).json()
                if response['result'] != 'pong':
                    print(datetime.now(), "Kodi not available via HTTP-transported JSON-RPC.  Waiting...")
                    time.sleep(5)
                else:
                    break
            except:
                time.sleep(5)
                pass

        print(datetime.now(), "Connected with Kodi.  Entering update_display() loop.")
        device.backlight(False)

        # Loop until Kodi goes away
        kodi_active = True
        screen_press = False
        while True:
            try:
                update_display()
            except (ConnectionRefusedError,
                    requests.exceptions.ConnectionError):
                print(datetime.now(), "Communication disrupted.")
                kodi_active = False
                break
            # This delay seems sufficient to have a (usually) smooth progress
            # bar and elapsed time update
            time.sleep(0.91)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if USE_TOUCH:
            print(datetime.now(), "Removing touchscreen interrupt")
            GPIO.remove_event_detect(TOUCH_INT)
        GPIO.cleanup()
        print(datetime.now(), "Stopping")
        exit(0)
