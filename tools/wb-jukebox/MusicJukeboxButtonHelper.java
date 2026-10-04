package com.wb.bdj.menu;
import com.wb.bdj.controller.x;
import com.wb.bdj.a.f;
public class MusicJukeboxButtonHelper extends SpecialFeatureClipButtonHelper {
    public MusicJukeboxButtonHelper(String[] args) { super(args); }
    public f[] a(l button) { return null; }
    public void a(l button, am actions) {
        k parent = button.q();
        if (parent instanceof com.wb.bdj.menu.a) ((com.wb.bdj.menu.a)parent).a((b)button);
        x.a().a(this.a, null);
        if (button instanceof be) bh.a(new BD2HEVCShowMusic((be)button, actions));
    }
}
class BD2HEVCShowMusic implements Runnable {
    private final be button;
    private final am actions;
    BD2HEVCShowMusic(be button, am actions) { this.button=button; this.actions=actions; }
    public void run() {
        System.err.println("BD2HEVC: showing music menu after state entry");
        actions.a(button.y);
        actions.a(button.z);
        actions.a(button.z.h());
        ((j)actions).g();
    }
}
