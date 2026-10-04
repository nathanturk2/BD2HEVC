package com.wb.bdj.controller;
import java.util.Properties;
public class BD2HEVCMusicJukeboxState extends MusicJukeboxState {
    public BD2HEVCMusicJukeboxState(String id, Properties properties) {
        super(id, properties);
        h = new BD2HEVCMusicTrackController(h.a, this, properties.getProperty(id + ".bd2hevc.playlists"), properties.getProperty(id + ".bd2hevc.offsets"));
    }
    public void a(q parameters) {
        System.err.println("BD2HEVC: music playlist deferred until song selection");
    }
    public void c(int track) {
        h.a(track - 1);
    }
    public boolean a(int key, com.wb.bdj.menu.am actions) {
        boolean handled = super.a(key, actions);
        super.y();
        return handled;
    }
    public void h() { ((BD2HEVCMusicTrackController)h).poll(); }
}
