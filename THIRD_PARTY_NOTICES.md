# Third-Party Notices

## Hadris UDF 2.2.0

BD2HEVC distributes the `hadris-udf` command-line utility for UDF 2.50 image
authoring and verification. The bundled binary includes a local MIT-licensed
fork for bounded-memory pipelined streaming, NTFS sparse staging, multi-extent
large-file authoring, and allocation-coverage verification. Its corresponding
source is distributed in `tools/hadris-udf/streaming-src/`.

- Project: https://github.com/hxyulin/hadris
- Author: hxyulin and contributors
- License: MIT

Copyright (c) hxyulin and contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The bundled Hadris author also carries the resolved Rust dependency licence
inventory and original licence/copyright texts in
[DEPENDENCY-LICENSES.json](tools/hadris-udf/DEPENDENCY-LICENSES.json) and
`tools/hadris-udf/dependency-licenses/`. Optional, development and other-platform
packages are included in that inventory; it is broader than the Windows binary.
