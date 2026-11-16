import json
from types import SimpleNamespace

import numpy as np
import pytest

from bladetag import walking3d
from bladetag.session import Session
from test_walking import synthetic_walk


def test_replay_preserves_stride_displacement_and_skips_gaps(tmp_path, monkeypatch):
    df, fs = synthetic_walk()
    df.loc[df.t_s > 12, 'segment'] = 1
    foot = SimpleNamespace(meta=SimpleNamespace(position='right_foot', address='test'))
    sess = Session(tmp_path, dict(start_elapsed_ns=0, activity='walking'), [foot], None)
    monkeypatch.setattr(walking3d, 'fit_clocks', lambda _: SimpleNamespace(rate_hz=fs))
    monkeypatch.setattr(walking3d, 'sample_frame', lambda *args: df)
    data = walking3d.build(sess, max_strides=10)
    assert len(data['cycles']) == 10
    for a, b in zip(data['cycles'], data['cycles'][1:]):
        if a['bout'] == b['bout']:
            assert b['t_s'] == pytest.approx(a['t_s']+a['period_s'], abs=.03)
    full = walking3d.build(sess)
    assert len(full['cycles']) > 10
    assert max(sum(c['bout'] == b['bout'] for c in full['cycles']) for b in full['cycles']) > 6
    for a, b in zip(full['cycles'], full['cycles'][1:]):
        contiguous = abs(b['t_s']-a['t_s']-a['period_s']) < .03
        assert (a['bout'] == b['bout']) == contiguous
    assert data['cycles'] == full['cycles'][:10]
    for c in data['cycles']:
        p, q = np.array(c['pos']), np.array(c['quat'])
        assert p.shape == (65, 3)
        assert p[-1, 0]-p[0, 0] == pytest.approx(1.4, rel=.04)
        assert np.linalg.norm(q, axis=1) == pytest.approx(np.ones(65), abs=2e-6)
        assert c['rpm'] == pytest.approx(120/c['period_s'])
        assert not c['t_s'] < 12 < c['t_s']+c['period_s']
    json.dumps(data, allow_nan=False)
    monkeypatch.setattr(walking3d, 'build', lambda *a, **kw: data.copy())
    out = tmp_path/'replay.html'
    walking3d.write_report(sess, out, '</script><script>alert(1)</script>')
    html = out.read_text()
    assert '__WALKING3D_DATA__' not in html
    assert '</script><script>alert(1)' not in html
    assert 'https://' not in html


def test_missing_foot(tmp_path):
    sess = Session(tmp_path, dict(start_elapsed_ns=0, activity='walking'), [], None)
    with pytest.raises(ValueError, match='labelled'):
        walking3d.build(sess)


def test_replay_interpolates_rotation_and_eases_stride_reference_changes(tmp_path):
    """Exercise the actual renderer's pose math across a stride boundary."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed to exercise the offline renderer')
    template = Path(walking3d.__file__).with_name('walking3d_template.html').read_text()
    script = template.split('<script>')[1].split('</script>')[0]
    # A 90-degree end pose followed by an independently neutral next stride.
    q = [0, 0, 2**-.5, 2**-.5]
    cycle = dict(pos=[[0, 0, 0], [1, 0, .1]], quat=[[0, 0, 0, 1], q],
                 period_s=1, t_s=0, rpm=120, bout=0, metrics={})
    data = dict(cycles=[cycle, dict(cycle, t_s=1)], typical={}, duration_s=2)
    harness = r'''
const vm=require('vm'),assert=require('assert');
const elements={},ctx=new Proxy({},{get:()=>()=>{}});
function el(id){return elements[id]??={value:id==='view'?'walk':'0.5',
 textContent:id==='motion-data'?JSON.stringify(DATA):'',
 getBoundingClientRect:()=>({width:1000,height:470}),getContext:()=>ctx,
 append(){},replaceChildren(){},addEventListener(){}};}
const sandbox={document:{getElementById:el,createElement:()=>el(Math.random()),addEventListener(){}},
 window:{addEventListener(){}},requestAnimationFrame(){},assert};
vm.createContext(sandbox);vm.runInContext(SCRIPT,sandbox);
vm.runInContext(`
 assert(Math.abs(dot(quaternion(placed[0],1),quaternion(placed[1],0)))>1-1e-8);
 assert(Math.abs(dot(quaternion(placed[1],1),C[1].quat.at(-1)))>1-1e-8);
 assert.deepStrictEqual(C[1].quat[0],[0,0,0,1]);
 // Moving across stride / metre boundaries must not relocate the lattice.
 const grid0=gridLines(true,.99),grid1=gridLines(true,1.01);
 assert.deepStrictEqual(grid0,grid1);
 for(const line of gridLines(true,10))for(const p of line)assert.strictEqual(p[2],-.08);
 const horizontal=grid=>grid.filter(([a,b])=>a[0]!==b[0]);
 assert.deepStrictEqual(horizontal(gridLines(true,0)),horizontal(gridLines(true,10)));
 // A long grid line crosses behind the camera; its visible portion must
 // project through the same intersection as the perpendicular grid line.
 const eye=[10,1,1],forward=unit([-1,-1,-1]),hit=[10,0,-.08];
 const clipped=clipSegment([-100,0,-.08],[100,0,-.08],eye,forward);
 assert(clipped);
 for(const p of clipped)assert(dot(sub(p,eye),forward)>=.05-1e-10);
 assert(Math.hypot(...cross(sub(hit,clipped[0]),sub(clipped[1],clipped[0])))<1e-8);
 assert.strictEqual(clipSegment([100,0,0],[101,0,0],eye,forward),null);
 const reverse=clipSegment([100,0,-.08],[-100,0,-.08],eye,forward);
 assert(Math.hypot(...sub(clipped[0],reverse[1]))<1e-8);
 assert(Math.hypot(...sub(clipped[1],reverse[0]))<1e-8);
 const half=quaternion(C[0],.5);
 assert(Math.abs(half[2]-Math.sin(Math.PI/8))<1e-8);
 assert(Math.abs(dot(slerp(half,mul(half,-1),.5),half))>1-1e-8);
`,sandbox);
'''
    source = 'const DATA=' + json.dumps(data) + ';const SCRIPT=' + json.dumps(script) + ';\n' + harness
    path = tmp_path/'rotation.cjs'
    path.write_text(source)
    subprocess.run([node, str(path)], check=True, capture_output=True, text=True)
