"""mkvprune — prune MKV audio/subtitle tracks and normalize audio to Dolby Digital.

The final mux is always performed with ``mkvmerge`` so that the video stream and all
Dolby Vision / HDR10 / HDR10+ / chapter / attachment / tag metadata survive bit-exact.
FFmpeg is used only to encode individual audio tracks into standalone intermediate files.
"""

__version__ = "0.2.0"
