# Local security analysis for Cale

This package connects a local copy of the official CVE List to source inspection,
dependency evidence and Ghidra binary analysis. The tools are available through Cale's normal specialist-tool discovery flow.

## How a security task works

1. Give Cale the source, dependency manifests or compiled files to inspect in the
   task folder. Describe the site or application and what you want checked.
2. Cale inventories the supplied files and records their hashes. It reads source
   and package declarations without running package installation hooks.
3. It searches the local CVE database for relevant advisories. Every result retains
   the original record digest and the upstream snapshot provenance.
4. For supported binaries, Ghidra produces disassembly and approximate decompiled
   code. The analyzed program is not launched. Long or unsupported analyses report
   their limits instead of being treated as complete.
5. Cale uses the evidence to investigate reachability, create a reproducible test,
   and check the fix. A keyword match or suspicious source pattern alone is not a
   verified vulnerability. A scan with no matches is not proof of safety.

Source inspection, binary analysis and testing answer different questions. A web
application can often be tested through its source, dependencies and observable
behavior without decompiling every component. Packed binaries, obfuscation,
missing server-side code and unsupported instruction sets can limit analysis.
No tool can promise to reverse-engineer every program.

## CVE data and storage

The source is [CVEProject/cvelistV5](https://github.com/CVEProject/cvelistV5).
The importer handles the official daily baseline and the hourly cumulative delta,
including published and rejected records and CNA/ADP data. An incomplete download
or index does not replace the last completed snapshot. Downloads are resumable;
published SHA-256 hashes are checked when available.

The database lives in `Knowledge/CVE/index.sqlite3` under the Workbench installation.
It is public vulnerability data, separate from private task memory. The first full
snapshot needs several gigabytes of disk space. Queries run locally. A refresh
needs internet access and respects Workbench's offline setting. Downloaded
advisory text, URLs and proof-of-concept references are treated as data, not run.

Retention removes only verified, inactive files owned by this cache. It preserves
the active baseline, current delta, incomplete imports and unknown files. Deleting
inactive database rows makes SQLite pages reusable; it does not immediately shrink
the database file. No automatic whole-disk indexing or client-data upload is added
by this package.

## Analysis backends

[Ghidra](https://github.com/NationalSecurityAgency/ghidra) supplies disassembly,
decompilation and program analysis. The adapter uses the existing pinned Ghidra
12.1.4 and JDK 21 installation, a fixed exporter and a bounded process family.
Stop/Pause and changes to the task's instructions invalidate ongoing analysis.
Reports remain in the task folder. These tools do not launch live website tests
or claim that a local file-analysis scope authorizes a remote scan.

## Packaging and development

The wheel contains readable Python source, a Java exporter resource and focused
tests. It has no installation scripts, import hooks, native executables, model
weights or bundled API keys. Its GitHub release URL is pinned by SHA-256 in the
Workbench's existing requirements file, so older updaters can install the package
before activating its toolbox wrappers.

The old updater installs dependencies before replacing application files. A source
update rollback does not uninstall this inert package. The installed self-check
must collect the packaged tests through the existing validation suite; packaging
tests is not itself evidence that they ran.

To rebuild the inert wheel from this checkout:

```powershell
python packages/cale-security/build_wheel.py --manifest packages/cale-security/source-manifest.json --version 0.1.1 --output dist
```

The manifest pins every input. Git preserves package bytes so the Java resource digest stays valid.
