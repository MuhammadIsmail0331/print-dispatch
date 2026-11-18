# Contributing

Useful issues contain a small public/reproducible manifest, the command, Python/OS
version, expected outcome and actual result. Do not upload private or unlicensed
CAD. Prefer a generated minimal mesh over a large production model.

Keep fixes focused, add a regression that reproduces the problem, and run:

```sh
python -m unittest discover -s tests -v
python examples/make_example.py
python print_dispatch.py check examples/job.json
```

Discuss new formats, geometric analyses or dependencies before implementing them.
Do not turn a digital integrity result into a strength/fit/printability certificate.
AI-assisted contributions are welcome when disclosed with concrete checks and
validation limits. Avoid generated issue/PR activity without a real problem.
