# README illustrations and example report

The animated walkthrough illustrates a terminal search, a sample listing and
an English report. It is a diagram of the workflow, rather than a recording of
an application screen. The report image comes from the actual report engine,
using explicitly synthetic data. The illustrated listing matches a seeded
2018 manual example: €14,328 and 109,600 km. Its asking price stays fixed.

Both animation layouts keep **ILLUSTRATIVE DEMO** visible throughout. Nothing
in these assets represents current inventory, verified vehicle condition,
transaction prices or national market coverage. The report sample has a DEMO
band on every PDF page; see [sample notes](sample-report-notes.md).

## Animation and accessible alternatives

- Desktop: `car-market-workflow.gif`, 1120 × 760.
- Mobile: `car-market-workflow-mobile.gif`, 640 × 820, with a separate layout
  and larger text relative to the screen.
- Static alternatives use the same filenames with `.png`.
- `sample-report-preview.png` is a cropped excerpt with the original DEMO band.
  The full overview and nine-page PDF are `sample-report.png` and `.pdf`.

The renderer draws a 13.2-second loop at 50 ms intervals. It types the command,
eases between scenes and reveals a second view of the report charts. A shared
palette keeps the type and colours stable. The README uses a responsive
`picture` element to select the mobile layout and a static poster when the
reader requests reduced motion. It also provides ordinary static-image links
and descriptive alt text.

GitHub documents responsive `picture` markup in its
[writing quickstart](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/quickstart-for-writing-on-github).
The width and reduced-motion queries follow browser media preferences. This
source markup does not establish a tested native GitHub mobile-app behavior.

## Reproduce

Use a source development environment with the existing reports extra and
`pypdf` development dependency. No extra package or image-generation API is
needed:

```sh
uv sync --frozen --group dev --extra reports
uv run --no-sync python docs/media/render_sample_report.py
uv run --no-sync python docs/media/render_workflow.py
```

For a layout check without rebuilding GIFs, use
`python docs/media/render_workflow.py --posters-only` in that environment.
The report helper uses disposable synthetic databases. The workflow renderer
reads only the example PNG and bundled fonts. Neither contacts a provider or
reads production data. Intermediate files stay outside the repository.

The data, example clocks and drawing timeline are fixed. Exact rendering bytes
depend on the pinned Pillow, plotting and font stack; future library versions
may change them. Project code and illustrations use MIT. Bundled Lato retains
its [SIL Open Font License](../../car_market/assets/fonts/OFL.txt).
