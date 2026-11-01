# reference/

Static lookup data the loaders read. Nothing here is written by a sync.

## tonal-movements.json (downloaded, not in the repo)

Tonal's export names movements only by UUID (`movementId`). A public catalog
of 286 movements maps those UUIDs to names. It isn't committed here: it is a
dump of Tonal's own API, coaching text and image links included, and has no
licence that would allow redistributing it. Fetch it instead:

```
python3 sync_all.py fetch-catalog
```

That downloads one pinned revision and installs it only if its hash matches:

- Source: "Tonal Movement Library", gist `aa79415d999696b50cc4aff9bc1f9f0c` by BrianVia
- Revision: `623fa360af417e73fe7015e76a269780d5d2e344`, committed 2025-02-03T02:24:19Z
- sha256: `208a9f7cc0ab546d6e066c867558604f5d21c722be6e8cb6861eaca939cda1bb`
- The newest `updatedAt` inside the file is 2024-02-01, so it describes Tonal's
  library as of early 2024. Movements Tonal added later aren't in it.

The URL and hash live in `wellness/tonal.py` (`CATALOG_URL`,
`CATALOG_SHA256`). To move to a newer revision, change both together.

Without the file, everything still works: the Tonal load warns that the
catalog wasn't loaded, and movements show by id until you name them below.
`sync_all.py demo` uses invented movement names unless the file is present.

The loader (`wellness/tonal.py`, `load_movements`) reads `id`, `name`,
`shortName`, `muscleGroups`, `bodyRegion`, `isTwoSided`, `isAlternating`,
`onMachine`, `skillLevel`, `onMachineInfo.accessory` and
`onMachineInfo.trainerArmsPulledAtSameTime` into `tonal_movement`.

Every Tonal load checks the catalog's `isTwoSided` against the sides seen in
the sets: a two-sided movement must have a side 1, and a one-sided movement
never may. A warning means either the catalog is stale or the export changed
shape.

## tonal-movement-names.toml (yours, edit freely)

Names for movement ids the catalog doesn't have. An entry here wins over the
catalog for the same id. `python3 sync_all.py status` lists the ids still
without a name, and the view `v_tonal_unknown_movements` has details for each
one. The names apply on the next `sync_all.py tonal` (or nightly) run.
