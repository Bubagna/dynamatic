"""Buffers per channel (before buffer placement) of two runs: bufdiff.py <comp A> <comp B>"""
import glob, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import closed_loop as cl, critical_path as cpa

def bufs(comp):
    dot = [p for p in glob.glob(comp + "/*.dot") if not p.endswith("_CFG.dot")][0]
    c = cpa.Circuit(dot, None, False)
    m = cl.ChannelMap(c, cl.PreBufferIR(comp + "/handshake_transformed.mlir"))
    bt = cl.buffer_types(comp + "/handshake_export.mlir")
    res = {}
    for i, e in enumerate(c.edges):
        if m.inserted(e["dst"]):
            ch = m.original(i)
            res.setdefault("%s:%d" % (ch["consumer"], ch["operand"]), []).append(bt.get(e["dst"]))
    return res

a, b = bufs(sys.argv[1]), bufs(sys.argv[2])
for k in sorted(set(a) | set(b)):
    if a.get(k) != b.get(k):
        print("%-22s A=%s B=%s" % (k, a.get(k), b.get(k)))
