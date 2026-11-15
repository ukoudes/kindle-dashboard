# Third-party notices

The macOS application bundles CPython and Pillow. Their licenses and copyright notices remain applicable; this project's own license status does not replace those terms.

- CPython: Python Software Foundation License and the other notices included in `Contents/Resources/runtime/lib/python3.12/LICENSE.txt`.
- Pillow: the license included in `Contents/Resources/runtime/lib/python3.12/site-packages/pillow-*.dist-info/licenses/LICENSE`. Pillow's binary dependencies and notices are documented by that distribution's metadata and SBOM, retained inside the bundle.
- The Python runtime dependency-license compilation is retained as `Contents/Resources/PYTHON-RUNTIME-LICENSES.rst`; its official upstream source is recorded next to it. It includes terms for the runtime dependencies, not an open-source license grant for this application.

CodexBar is an optional, separately installed application. It is not included in this distribution. System frameworks and system fonts are supplied by macOS and are not copied into the release.

Upstream references:

- https://docs.python.org/3/license.html
- https://github.com/python-pillow/Pillow/blob/main/LICENSE
- https://github.com/astral-sh/python-build-standalone/blob/main/docs/distributions.rst
