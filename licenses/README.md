# Third-Party Licenses

This directory contains the full license texts for all third-party dependencies
used in the MyVector project.

## Included Licenses

### Apache License 2.0

- **File:** [LICENSE-Apache-2.0.txt](LICENSE-Apache-2.0.txt)
- **Used by:** HNSWlib (Hierarchical Navigable Small World)
- **Source:** <https://github.com/nmslib/hnswlib>
- **Purpose:** High-performance Approximate Nearest Neighbor (ANN) search

### Boost Software License 1.0

- **File:** [LICENSE-Boost-1.0.txt](LICENSE-Boost-1.0.txt)
- **Used by:** Boost C++ Libraries
- **Source:** <https://www.boost.org/>
- **Purpose:** C++ utility libraries (headers provided by MySQL build environment)

### GNU General Public License v2.0

- **File:** See [../LICENSE](../LICENSE) in the root directory
- **Used by:**
  - MyVector (main project)
  - MySQL Server
- **Purpose:** Main project license and MySQL plugin interface

## License Compatibility

All third-party licenses listed here are compatible with the main project license
(GPL v2.0):

- Apache 2.0 → GPL v2.0: ⚠️ Not compatible with GPL v2-only; compatible only
  if GPL code is "GPL v2 or later" (allowing upgrade to GPL v3) or GPL v3
- Boost 1.0 → GPL v2.0: ✅ Compatible (permissive to copyleft)
- GPL v2.0 → GPL v2.0: ✅ Compatible (same license)

For more information about licensing and attribution, see:

- [../NOTICE](../NOTICE) - Third-party attribution notices
- [../LICENSE](../LICENSE) - Main project license
