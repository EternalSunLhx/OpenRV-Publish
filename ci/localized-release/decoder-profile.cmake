# Release decoding support, using OpenRV's existing per-codec opt-in.
# Keep hardware/OS/third-party SDK implementations and encoder defaults unchanged.
set(RV_FFMPEG_NON_FREE_DECODERS_TO_ENABLE
    "aac;aac_fixed;aac_latm;ac3;ac3_fixed;eac3;bink;binkaudio_dct;binkaudio_rdft;dnxhd;dvvideo;prores;prores_raw;qtrle;vp9;hevc;mpeg2video;svq1;svq3"
    CACHE STRING "Native software decoders included in community release packages" FORCE)
set(RV_FFMPEG_NON_FREE_ENCODERS_TO_ENABLE ""
    CACHE STRING "Keep upstream encoder defaults for release packages" FORCE)
