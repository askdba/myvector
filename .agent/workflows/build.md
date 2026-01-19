---
description: How to build MyVector plugin locally against MySQL 8.4
---

This workflow describes the process of setting up a local build environment and building the MyVector plugin.

// turbo-all
1. **Clone MySQL Server**
   Clone the MySQL 8.4.0 source code to a sibling directory.
   ```bash
   cd ..
   git clone --depth 1 -b mysql-8.4.0 https://github.com/mysql/mysql-server.git
   ```

2. **Prepare Plugin Directory**
   Create a symlink for MyVector inside the MySQL plugin directory.
   ```bash
   cd mysql-server/plugin
   ln -s ../../myvector myvector
   ```

3. **Install Dependencies**
   Ensure all build dependencies are installed via Homebrew.
   ```bash
   brew install cmake bison openssl@3 ncurses
   ```

4. **Configure and Build**
   Create a build directory and run CMake.
   ```bash
   cd ../
   mkdir bld
   cd bld
   cmake .. -DDOWNLOAD_BOOST=1 -DWITH_BOOST=../boost -DFORCE_INSOURCE_BUILD=1 -DOPENSSL_ROOT_DIR=$(brew --prefix openssl@3)
   make myvector
   ```
