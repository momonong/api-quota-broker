#!/bin/sh
set -eu
test "$#" -eq 0
exec sudo /usr/bin/python3.14 -I -B -S -c 'import hashlib,os,stat,tempfile,subprocess,sys
from pathlib import Path
pins={'"'"'ops_entry.py'"'"': '"'"'bb7aa9929aca692f926142aabe48e7c9955d86061d6f62d3ead030fbe81bc797'"'"', '"'"'broker_ops_policy.py'"'"': '"'"'30784138d7824514c4de69222ff0992905782e2da60f4468f368bd495a6b0522'"'"', '"'"'history_audit_protocol.py'"'"': '"'"'71f67ee04b78a759b806fbaf43b06bab086dc2a059cad9696cdf8f24b5278c48'"'"', '"'"'history_audit_reader.py'"'"': '"'"'f191b0cc1f248e363b9b8e2c11f8372ed1b261d5c8eb3981a5a106d7e56f3da6'"'"', '"'"'ops_history_projection.py'"'"': '"'"'c5da3993c770180e7ad1de7241fcbce40ab028d4949eef1a612ad75277fc66a3'"'"', '"'"'maintenance_ops.py'"'"': '"'"'ab7a3faa1da962bb145b020b67ebc15c29f28de379827d94b3148796b8be975e'"'"', '"'"'maintenance_protocol.py'"'"': '"'"'00721b5ebba4f207aacf28c5007542685280ad0475f63af1d12ca7f08c71956c'"'"', '"'"'export_client_credential.py'"'"': '"'"'e63e04ab45418a82fc32d56522c6645c95dd2dedbc1e84e2c14e0a2112a517d5'"'"', '"'"'aqb'"'"': '"'"'cc1b6c9897010f414bd3d03e95831a3b4284c731938fc017f8e93bd4023102a1'"'"', '"'"'bootstrap_maintenance.py'"'"': '"'"'3200960cb2e51e3e1cf78a362ebcbfe9d753dca5be790b273d36734827997112'"'"', '"'"'verify_maintenance_offline.py'"'"': '"'"'bc62851743d9fb44dca29c39b268142d8096d4db9ef21b82310dbe0287e0530a'"'"', '"'"'release_verifier.py'"'"': '"'"'fc99a704d01722244a7d445acdeaec3697c8acac04935da54ef5263efd1115d8'"'"', '"'"'project.whl'"'"': '"'"'558226fef82ddac2e8af299031ffaf492ee7b8b2480ef56344733673044b3fe7'"'"', '"'"'broker-v1.service'"'"': '"'"'55e36fc717a1b143dd65bfedc629aa647a08043ab151fb6986ab884fed3261b3'"'"', '"'"'client-v1.service'"'"': '"'"'158d1a2de7b00b6f2d43bb8608be55a1143e92ad29bc6e182f155f38eb9d7709'"'"', '"'"'api-quota-broker-ops@.service'"'"': '"'"'7280e5038f01e057ac104b90aa83d83e1d6588105d9823171e6861111464ce94'"'"', '"'"'api-quota-broker-history-audit@.service'"'"': '"'"'f22d281518f9de5e312e9c390e93578e3675e2fd4d0ccc49ca416c66103514c5'"'"', '"'"'api-quota-broker-maintenance@.service'"'"': '"'"'8797208ecd97ecfb8e0cb30f9d9d52060a560b79d29ac88eb214a4e1d4eca25d'"'"', '"'"'api-quota-broker-ops-client'"'"': '"'"'399d0e02dbc30960c1ea846cda708502001d0e61bf01062b072c98f71dba1563'"'"', '"'"'maintenance-profile.json'"'"': '"'"'5cdc6f2d3cc6939b68f1b75fb4db18f9b8da7d2830e9f51d52b53d146bd5ceb8'"'"', '"'"'active-release.json'"'"': '"'"'29f490e532a041544e223a603162800b4289fc6fbd124f7b71574d7be5c0afce'"'"', '"'"'manifest.json'"'"': '"'"'da067b47a50438d958b5fdc2de381525fc4dc724e3b47a6ec7e6351b6e2b3a0a'"'"', '"'"'seal.json'"'"': '"'"'3bd96c2b28af5b859e0b14289d2305f2028a988e2297e5e475bdfbf0412121c1'"'"'}
source=Path('"'"'/var/tmp/api-quota-broker-maintenance-review-2026-10-09-r1'"'"')
root=Path(tempfile.mkdtemp(prefix='"'"'aqb-maintenance-bootstrap-'"'"',dir='"'"'/var/tmp'"'"'))
for name,sha in pins.items():
 fd=os.open(source/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'"'"'rb'"'"') as stream:
  s=os.fstat(stream.fileno())
  assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_uid==1000 and s.st_size<=2097152
  data=stream.read(2097153)
  assert len(data)==s.st_size and hashlib.sha256(data).hexdigest()==sha
 with open(root/name,'"'"'xb'"'"') as out:
  os.chmod(root/name,0o600);out.write(data);out.flush();os.fsync(out.fileno())
result=subprocess.run(['"'"'/usr/bin/python3.14'"'"','"'"'-I'"'"','"'"'-B'"'"','"'"'-S'"'"',str(root/'"'"'bootstrap_maintenance.py'"'"')],env={'"'"'PATH'"'"':'"'"'/usr/bin:/bin'"'"','"'"'LANG'"'"':'"'"'C'"'"'})
sys.exit(result.returncode)
'
