# Project Structure

This repository follows a standard layout inspired by `mysql-mcp-server`
and modern C++ projects.

## Directory Layout

- **`src/`**: Source code (`.cc` files) for the MySQL plugin and related logic.
- **`include/`**: Header files (`.h` files) defining interfaces and data structures.
- **`sql/`**: SQL scripts for installation, testing, and schema definitions.
- **`docs/`**: Documentation files, including benchmark results and guides.
- **`examples/`**: Example datasets and demo scripts (e.g., `stanford50d`).
- **`cmake/`**: (Optional) Additional CMake modules.
- **`build/`**: (Generated) Build artifacts.

## Key Files

- `CMakeLists.txt`: Build configuration for CMake.
- `Makefile`: simplified build commands (`make build`, `make install`).
- `CHANGELOG.md`: Version history and notable changes.
