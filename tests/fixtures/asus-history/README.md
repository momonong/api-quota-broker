# Public historical regression fixtures

These are test inputs, not native acceptance receipts or current account admission.
The review candidates were captured from uncommitted public source while repository
HEAD was `810da023eb796b67faa2dffca93c5325ab22e035`; HEAD is provenance only.
The exact historical origin identity is the review path plus SHA-256 below, not
a claim that this source was committed at that HEAD. No archive, wheel, credential,
raw host receipt, or service state is included. Original evidence stays untouched.

- r11 public modules preserve the fixed retained-state hashes used by current installer gates.
- r12 installer preserves the original whole-preflight ordering and generic-receipt regression; it is loaded only against a virtual host. The complete module is required for its NativeBootstrap transaction methods.
- maintenance-r2 public module/manifest/unit preserve the fixed repair precondition hashes.
- normal-r2-release-manifest is only the canonical manifest extracted from the historical archive; repair tests assert its existing production pin.
- bootstrap-r1 preserves the wrapper pin and exact sudo command parser regression.
- seven-r2-broker.service is only the original service text needed for partial-write rollback.
- seven-r2-admission preserves existing public synthetic declaration fields/times used in config identity and quota tests. It contains key-reference names, never key values.
- r8 projections retain only the five public recovery receipt fields and the exact nanosecond timestamp. The numeric variant is a derived minimal input for the integer/string regression, not a raw host receipt.

| Fixture | Origin under docs/evidence | Bytes | SHA-256 |
|---|---|---:|---|
| `r11/ops_entry.py` | `asus-ops/r11-recovery-review/ops_entry.py` | 27048 | `9d3a7ff14f2e7b3ec613bf2f6191a5d8cc97c1a9f7446e7ee30e09ce41953a61` |
| `r11/broker_ops_policy.py` | `asus-ops/r11-recovery-review/broker_ops_policy.py` | 10546 | `30784138d7824514c4de69222ff0992905782e2da60f4468f368bd495a6b0522` |
| `r12/install_ops.py` | `asus-ops/r12-diagnostic-final-v2-review/install_ops.py` | 98705 | `658a11c7c833d4d615f4837e49c0f46713abe94170e66cad190d72691bc974ab` |
| `maintenance-r2/maintenance_ops.py` | `asus-maintenance/review-2026-10-09-r2/maintenance_ops.py` | 36861 | `ab7a3faa1da962bb145b020b67ebc15c29f28de379827d94b3148796b8be975e` |
| `maintenance-r2/manifest.json` | `asus-maintenance/review-2026-10-09-r2/manifest.json` | 1628 | `d6865f3bff7bb7410b1814e8fbdbd2fd310c3d1bc4b4f7d542286c08a8d4a86a` |
| `maintenance-r2/api-quota-broker-maintenance@.service` | `asus-maintenance/review-2026-10-09-r2/api-quota-broker-maintenance@.service` | 1078 | `8797208ecd97ecfb8e0cb30f9d9d52060a560b79d29ac88eb214a4e1d4eca25d` |
| `bootstrap-r1.sh` | `asus-maintenance/review-2026-10-09-r1/bootstrap-once.sh` | 3572 | `afbc1db8e1596491d4d556d874eff7ba1a249982934c76f908cfd82d81ad7560` |
| `normal-r2-release-manifest.json` | `asus-normal-v1/review-2026-10-09-r2/source.tar#release-manifest.json` | 8776 | `20ef3b817a17aeb2c7315c4dc94a34aaa52ee92b740e6b8f134d6cebdf1a5855` |
| `seven-r2-broker.service` | `asus-seven-pool/review-2026-10-08-r2/source.tar#deploy/asus/api-quota-broker.service` | 2629 | `6596ebca936ef42b8ff12d5bb25cdd2baf395791f172ee8403948006778ec060` |
| `seven-r2-admission.json` | `asus-seven-pool/review-2026-10-08-r2/policy.json#admission (public subset)` | 4250 | `b059dcdd0baba616a4b5aa37aa7be5ec32a4e18c9a7dbff437326aaa38f79f75` |
| `r8-prepared-projection.json` | `asus-ops/r8-prepared-projection-2026-10-06.json#projection.receipts[0] (public subset)` | 872 | `0eff78a610060740855c8c253f6c46e7208e1ce685e15771ed11ac278856b3bd` |
| `r8-prepared-projection.numeric.json` | `derived from r8-prepared-projection.json by converting mtime_ns to an integer` | 870 | `6a4094f655ca0881025ead90d42bbb9a36c3a1954299bb0c13d699695368795b` |
