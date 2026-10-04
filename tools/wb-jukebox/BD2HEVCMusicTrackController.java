package com.wb.bdj.controller;
import java.util.Timer;
import java.util.TimerTask;
class BD2HEVCMusicTrackController extends k {
    private final int[] playlists;
    private int generation;
    BD2HEVCMusicTrackController(a[] tracks, w listener, String playlistList, String offsetList) {
        super(tracks, listener);
        String[] ids=playlistList.split(","); String[] offsets=offsetList.split(",");
        playlists = new int[tracks.length];
        for(int i=0;i<tracks.length;i++) {
            int item=tracks[i].b-1;
            playlists[i]=Integer.parseInt(ids[item]);
            a[i]=new a(tracks[i].a, 1, 1, tracks[i].d-Long.parseLong(offsets[item]));
        }
    }
    public synchronized void a(int index) {
        if (index < 0 || index >= a.length) return;
        if (c != null) { c.cancel(); c = null; }
        generation++;
        x player = x.a();
        try {
            player.g.stop();
            player.k.selectPlayList(new org.bluray.net.BDLocator(null, -1, playlists[index]));
            player.g.prefetch();
            player.l.selectStreamNumber(a[index].a);
            player.g.setRate(1.0f);
            player.g.start();
            b = index;
            d.d(index);
            c = new Timer();
            c.scheduleAtFixedRate(new BD2HEVCAdvanceMusic(this, generation), 200L, 200L);
            System.err.println("BD2HEVC: playing track " + (index + 1) + " playlist " + playlists[index] + " stream " + a[index].a);
        } catch (Exception error) { b(); error.printStackTrace(); }
    }
    public synchronized void b() { generation++; super.b(); }
    synchronized void poll(int token) { if (token == generation) poll(); }
    synchronized void poll() {
        if (b < 0 || x.a().g.getMediaNanoseconds() < a[b].d) return;
        if (b + 1 < a.length) a(b + 1);
        else { b(); d.y(); }
    }
}
class BD2HEVCAdvanceMusic extends TimerTask {
    private final BD2HEVCMusicTrackController player;
    private final int generation;
    BD2HEVCAdvanceMusic(BD2HEVCMusicTrackController player, int generation) { this.player=player; this.generation=generation; }
    public void run() { player.poll(generation); }
}
