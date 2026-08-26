#!/usr/bin/env python3
"""End-to-End-Mesh-Test: 2 FreeTetra-Server (in-process) + 2 echte BREW-Clients.
Prueft: Trunk-Auth (Shared Secret), Interest-Propagation, TG-Routing ueber
Servergrenzen (beide Richtungen), dass un-besessene TGs lokal bleiben, und dass
ein falsches Secret abgewiesen wird.  Aufruf:  python3 test_mesh.py"""
import asyncio, hashlib, os, re, secrets, struct, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server, websockets

TMP = tempfile.mkdtemp(prefix="brewmesh-")

def md5(s): return hashlib.md5(s.encode()).hexdigest()
def u16(v): return struct.pack("<H", v)
def u32(v): return struct.pack("<I", v)
def u64(v): return struct.pack("<Q", v)
def m_register(i): return bytes([0xF0,0x01])+u32(i)+u64(0)+u32(0)
def m_affil(i,g): return bytes([0xF0,0x08])+u32(i)+u64(0)+u32(0)+u16(1)+u32(g)
def m_grouptx(uu,s,g): return bytes([0xF1,0x02])+uu+u32(s)+u32(g)+b"\x00\x00"+u16(0)
def m_frame(uu): return bytes([0xF2,0x00])+uu+u16(0)+b"\x00"+b"A"*35
def m_idle(uu): return bytes([0xF1,0x03])+uu+b"\x00"

def digest(user, pw, nonce, uri="/brew/"):
    ha1=md5(f"{user}:brew:{pw}"); ha2=md5(f"GET:{uri}")
    nc="00000001"; cn=secrets.token_hex(8)
    r=md5(f"{ha1}:{nonce}:{nc}:{cn}:auth:{ha2}")
    return (f'Digest username="{user}", realm="brew", nonce="{nonce}", uri="{uri}", '
            f'qop=auth, nc={nc}, cnonce="{cn}", response="{r}", algorithm=MD5')

async def brew_connect(port, user, pw="freetetra"):
    base=f"ws://127.0.0.1:{port}"
    try:
        async with websockets.connect(base+"/brew/", subprotocols=["brew"]): pass
        raise RuntimeError("401 erwartet")
    except websockets.exceptions.InvalidStatus as e:
        assert e.response.status_code==401
        nonce=re.search(r'nonce="([^"]+)"', e.response.headers["WWW-Authenticate"]).group(1)
    try:
        async with websockets.connect(base+"/brew/",
                additional_headers={"Authorization":digest(user,pw,nonce)}, subprotocols=["brew"]): pass
        raise RuntimeError("200 erwartet")
    except websockets.exceptions.InvalidStatus as e:
        assert e.response.status_code==200, e.response.status_code
        sess=bytes(e.response.body).decode()
    return await websockets.connect(base+sess, subprotocols=["brew"])

async def start(port, local_prefix, peer_url, peer_prefix, secret="s3cr3t"):
    cfg={"server":{"host":"127.0.0.1","port":port},"open":True,"open_password":"freetetra",
         "web_dir":TMP,"radioid_cache":TMP+f"/rc{port}.json",
         "mesh":{"local_prefix":local_prefix,"peers":[
             {"name":"T","secret":secret,"prefix":peer_prefix,"url":peer_url}]}}
    s=server.BrewServer(cfg)
    await websockets.serve(s.ws_handler,"127.0.0.1",port,process_request=s.process_request,subprotocols=["brew"])
    asyncio.ensure_future(s.mesh_loop())
    for p in s.peers.values():
        if p.url: asyncio.ensure_future(s.trunk_dial(p))
    return s

async def recv(ws,n=3,t=3):
    got=[]
    try:
        for _ in range(n): got.append(await asyncio.wait_for(ws.recv(),timeout=t))
    except asyncio.TimeoutError: pass
    return got

async def main():
    A=await start(8801,"262","ws://127.0.0.1:8802/trunk","263")
    B=await start(8802,"263","ws://127.0.0.1:8801/trunk","262")
    await asyncio.sleep(1.5)
    assert A.peers["T"].connected() and B.peers["T"].connected()
    print("1) Trunk verbunden + authentifiziert  OK")

    CA=await brew_connect(8801,"2621001"); await CA.send(m_register(2621001)); await CA.send(m_affil(2621001,2635))
    CB=await brew_connect(8802,"2635001"); await CB.send(m_register(2635001)); await CB.send(m_affil(2635001,2621))
    await asyncio.sleep(4.5)
    assert 2635 in B.peers["T"].interest and 2621 in A.peers["T"].interest
    print("2) Interest ueber Trunk propagiert  OK")

    uu=b"\x11"*16
    await CB.send(m_grouptx(uu,2635001,2635)); await CB.send(m_frame(uu)); await CB.send(m_idle(uu))
    g=await recv(CA)
    assert any(m[0]==0xF1 and m[1]==0x02 for m in g) and any(m[0]==0xF2 for m in g)
    print("3) B->A: Ruf auf 2635 (Owner B) kommt bei A an  OK")

    uu2=b"\x22"*16
    await CA.send(m_grouptx(uu2,2621001,2621)); await CA.send(m_frame(uu2)); await CA.send(m_idle(uu2))
    assert any(m[0]==0xF1 and m[1]==0x02 for m in await recv(CB))
    print("4) A->B: Ruf auf 2621 (Owner A) kommt bei B an  OK")

    CB2=await brew_connect(8802,"2699001"); await CB2.send(m_register(2699001)); await CB2.send(m_affil(2699001,8))
    await asyncio.sleep(4.5)
    uu3=b"\x33"*16
    await CA.send(m_grouptx(uu3,2621001,8)); await CA.send(m_frame(uu3))
    assert not any(m[0]==0xF1 for m in await recv(CB2,2,2))
    print("5) TG ohne Vorwahl-Owner bleibt lokal (nicht gemesht)  OK")

    D=await start(8804,"264","ws://127.0.0.1:8801/trunk","262",secret="FALSCH")
    await asyncio.sleep(2.5)
    assert not D.peers["T"].connected()
    print("6) Falsches Secret -> kein Trunk (Fremd-Meshing abgewehrt)  OK")

    print("\nALLE MESH-TESTS BESTANDEN.")

if __name__ == "__main__":
    asyncio.run(main())
