"""Rename audio endpoints the way the Sound control panel does: IMMDevice.OpenPropertyStore(STGM_READWRITE) + SetValue.
Run elevated:  python vcable_rename.py "CABLE Input=Teams transcriber" "CABLE Output=Teams transcriber (захват)"
Writes result lines to vcable_rename.log next to this file."""
import ctypes, os, sys, traceback
from ctypes import wintypes
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vcable_rename.log")
def log(m):
    with open(LOG, "a", encoding="utf-8") as f: f.write(m + "\n")
try:
    import comtypes
    from comtypes import GUID, CLSCTX_ALL, COMMETHOD, HRESULT, POINTER, IUnknown, cast
    from pycaw.pycaw import AudioUtilities
    from pycaw.api.mmdeviceapi import IMMDevice
    from pycaw.api.mmdeviceapi.depend.structures import PROPERTYKEY
    from pycaw.api.mmdeviceapi.depend import IPropertyStore  # type: ignore
    STGM_READWRITE = 0x2
    PKEY_FRIENDLY = PROPERTYKEY()
    PKEY_FRIENDLY.fmtid = GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"); PKEY_FRIENDLY.pid = 2
    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ubyte), ("r2", ctypes.c_ubyte), ("r3", ctypes.c_ulong),
                    ("pwszVal", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]
    # IPropertyStore with SetValue/Commit (pycaw's version may lack them)
    class IPropertyStoreW(IUnknown):
        _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.DWORD), "cProps")),
            COMMETHOD([], HRESULT, "GetAt", (["in"], wintypes.DWORD, "iProp"), (["out"], POINTER(PROPERTYKEY), "pkey")),
            COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"), (["out"], POINTER(PROPVARIANT), "pv")),
            COMMETHOD([], HRESULT, "SetValue", (["in"], POINTER(PROPERTYKEY), "key"), (["in"], POINTER(PROPVARIANT), "pv")),
            COMMETHOD([], HRESULT, "Commit"),
        ]
    pairs = dict(a.split("=", 1) for a in sys.argv[1:])
    log(f"elevated={bool(ctypes.windll.shell32.IsUserAnAdmin())} pairs={pairs}")
    for d in AudioUtilities.GetAllDevices():
        name = d.FriendlyName or ""
        for old, new in pairs.items():
            if name.startswith(old):
                dev = d._dev
                # OpenPropertyStore returns a POINTER(IPropertyStore) in pycaw's binding; re-cast to ours
                store = dev.OpenPropertyStore(STGM_READWRITE)
                store = store.QueryInterface(IPropertyStoreW)
                pv = PROPVARIANT(); pv.vt = 31; pv.pwszVal = new  # VT_LPWSTR
                hr = store.SetValue(PKEY_FRIENDLY, pv)
                hr2 = store.Commit()
                log(f"{name!r} -> {new!r}: SetValue={hr} Commit={hr2}")
    log("done")
except Exception:
    log("EXC " + traceback.format_exc())
