import json, time, sys
from pathlib import Path
ROOT=Path(__file__).parents[2]; sys.path.insert(0,str(ROOT))
from src.jobs.manager import JobManager

root = Path(r"D:\comsol_runtime\h2d_real\jobs")
m = JobManager(root, cancel_grace_seconds=10, cancel_terminate_seconds=2)
r = m.submit({"job_type":"staged_sweep","source_model_path":r"C:\Users\陆星\Desktop\iterations\Zhou2025_QBIC\stage2_localmesh.mph","parameter_name":"wl","parameter_unit":"um","parameter_values":[4.252,4.254],"expressions":["ewfd.Rtotal","ewfd.Ttotal","ewfd.Atotal","ewfd.Rtotal+ewfd.Ttotal+ewfd.Atotal"],"study_name":"std1","version":"6.4","cores":14,"smoke_points":1,"record_wavelength_controls":True,"physical_bounds":{"ewfd.Rtotal":[0,1.001],"ewfd.Ttotal":[0,1.001],"ewfd.Atotal":[0,1.001],"ewfd.Rtotal+ewfd.Ttotal+ewfd.Atotal":[0.999,1.001]}})
job=r["job_id"]
deadline=time.time()+150
while time.time()<deadline:
    s=m.status(job)
    if s["status"]=="running":
        print(json.dumps(m.cancel(job)), flush=True)
        break
    time.sleep(.2)
while time.time()<deadline:
    s=m.status(job)
    if s["status"] in {"cancelled","failed","interrupted","completed"}:
        print(json.dumps(s), flush=True); break
    time.sleep(.2)
else: raise SystemExit("timeout")
