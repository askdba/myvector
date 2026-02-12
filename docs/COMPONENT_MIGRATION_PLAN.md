### **Objective**

To migrate the existing `myvector` MySQL plugin to the MySQL Component Infrastructure. This will improve modularity, align with modern MySQL architecture, and ensure future compatibility, as the traditional plugin infrastructure is being deprecated.

### **1. Analysis of the Existing Plugin Architecture**

The current implementation is a multi-purpose audit plugin with the following key functionalities:

*   **Query Rewriting:** It uses the `MYSQL_AUDIT_PARSE_PREPARSE` audit event hook in `myvector_plugin.cc` to intercept and rewrite SQL statements containing `MYVECTOR` annotations (e.g., `CREATE TABLE ... MYVECTOR(...)`, `... WHERE MYVECTOR_IS_ANN(...)`).
*   **User-Defined Functions (UDFs):** It registers several UDFs, whose logic resides in `myvector.cc`. These include `myvector_construct`, `myvector_display`, `myvector_distance`, and `myvector_ann_set`, which are the primary user interface for vector operations.
*   **Background Binlog Processing:** It starts a background thread (`myvector_binlog_loop` in `myvector_binlog.cc`) to monitor the binary log. This enables "online" vector indexes to be updated automatically in response to DML operations on base tables.
*   **Configuration:** It uses global system variables (e.g., `myvector_index_dir`, `myvector_config_file`) declared with `MYSQL_SYSVAR` for configuration.
*   **Build System:** It uses the `MYSQL_ADD_PLUGIN` macro in `CMakeLists.txt` for compilation and linking.

### **2. Proposed Component Architecture**

The monolithic plugin will be decomposed into a single component that provides several distinct services. This modular approach is the core benefit of the component infrastructure.

*   **Main Component (`myvector_component.cc`):** This will be the main entry point. It will manage the component's lifecycle (`init`/`deinit`) and register the various services below.
*   **Manifest File (`myvector.json`):** A required JSON file that describes the component to the MySQL server.
*   **Query Rewriter Service:** A dedicated service implementing the `mysql::Query_rewriter_service` interface. This is the modern, official way to perform query rewriting, replacing the general-purpose audit hook.
*   **Binlog Monitoring Service:** A background service that encapsulates the binlog monitoring thread. Its lifecycle (start/stop) will be managed by the main component. All state (MySQL connection, binlog position, etc.) will be contained within this service.
*   **UDF Service:** A service responsible for registering and deregistering all `myvector` UDFs using the `mysql_udf_metadata` service provided by the server.

### **3. Detailed Implementation Plan**

Here is a step-by-step plan for the migration:

**Step 1: Project Scaffolding**

1.  **Create New Directory:** Create a new directory, `src/component_src`, to house all new files related to the component. This keeps the new architecture separate from the legacy plugin code during development.
2.  **Create Manifest File:** Create `src/component_src/myvector.json`. This file will contain essential metadata.
    ```json
    {
      "name": "myvector",
      "description": "Vector Storage & Search Component for MySQL",
      "license": "GPL",
      "version": "1.3.0"
    }
    ```

**Step 2: Core Component and Service Implementation**

1.  **Main Component File:** Create `src/component_src/myvector_component.cc`. This file will contain the component's main lifecycle functions (`myvector_component_init`, `myvector_component_deinit`) and the component declaration (`mysql_declare_component`).
2.  **Query Rewriter Service:**
    *   Create `src/component_src/myvector_query_rewrite_service.cc`.
    *   Implement a class that inherits from `mysql::Query_rewriter_service`.
    *   Move the query rewriting logic from `myvector_sql_preparse` into the `rewrite_query` method of this new class.
    *   Register the service using the `SERVICE_REGISTRATION` macro.
3.  **Binlog Monitoring Service:**
    *   Create `src/component_src/myvector_binlog_service.h` to define the service interface with `start_binlog_monitoring()` and `stop_binlog_monitoring()` methods.
    *   Create `src/component_src/myvector_binlog_service.cc`.
    *   Move the logic from `myvector_binlog_loop` and related functions/globals from `src/myvector_binlog.cc` into the implementation of this service class. The thread and its state will become private members of the class.
    *   The `start` method will launch the thread, and `stop` will signal it to terminate and join.
4.  **UDF Service:**
    *   Create `src/component_src/myvector_udf_service.h` to define the service interface.
    *   Create `src/component_src/myvector_udf_service.cc`.
    *   Move all UDF implementation functions from `src/myvector.cc` into this file.
    *   Remove the `PLUGIN_EXPORT` macros.
    *   Implement `register_udfs` and `deregister_udfs` methods that use the server's `mysql_udf_metadata` service to manage the UDFs.

**Step 3: Integration and Refactoring**

1.  **Integrate Services:** Update `src/component_src/myvector_component.cc`:
    *   In `myvector_component_init`, get the `mysql_udf_metadata` service and call the `register_udfs` method from the UDF service. Then, call the `start_binlog_monitoring` method from the binlog service.
    *   In `myvector_component_deinit`, call `deregister_udfs` and `stop_binlog_monitoring`.
    *   Add all implemented services to the `mysql_declare_component` descriptor block.
2.  **Refactor Configuration:**
    *   Preserve `MYSQL_SYSVAR` declarations to keep configuration dynamic and compatible with `SET GLOBAL`.
    *   The configuration file (`myvector.cnf`) loading logic will be explicitly called during the component's `init` phase, with sysvar defaults set from the file when available.
    *   Services will read configuration through sysvars to avoid duplicated state.

**Step 4: Update the Build System**

1.  **Modify `CMakeLists.txt`:**
    *   Remove the `MYSQL_ADD_PLUGIN(...)` block.
    *   Add a new `add_library(myvector_component SHARED ...)` command.
    *   The source files for the library will include all new `src/component_src/*.cc` files plus the existing core logic files (`src/myvector.cc`, `src/myvectorutils.cc`, etc.). The old `src/myvector_plugin.cc` will be excluded.
    *   Add `install(TARGETS myvector_component ...)` to install the shared library to the correct MySQL directory (e.g., `lib/plugin`).
    *   Add `install(FILES src/component_src/myvector.json ...)` to install the manifest file.

**Building the component (Step 4 complete)**

Component headers live in the MySQL **server** source tree. Configure and build with:

*   **Required:** `MYSQL_SOURCE_DIR` — path to MySQL server source root (must contain `include/mysql/components/component_implementation.h`).
*   **Optional:** `MYSQL_BUILD_DIR` — server build dir for generated headers; `MYSQL_DIR` — MySQL install root to find `libmysqlclient`.

Example:

```bash
mkdir build && cd build
cmake -DMYSQL_SOURCE_DIR=/path/to/mysql-server ..
make
make install
```

**Script (clone or use existing source):** `scripts/build-component.sh [mysql-version] [mysql-source-dir]`. With no args it clones `mysql-8.4.8` into a temp dir and builds. Pass an existing path to use your own clone: `./scripts/build-component.sh mysql-8.4.8 /path/to/mysql-server`.

**Step 5: Testing Strategy**

1.  **Build:** Compile the project using `cmake` and `make`.
2.  **Installation:** In a test MySQL 8.0+ instance, place the compiled `myvector_component.so` and `myvector.json` in the appropriate directories.
3.  **Activation:** Connect to MySQL and run `INSTALL COMPONENT 'file://myvector'`. Verify success by checking the MySQL error log and querying the `mysql.component` table.
4.  **Functional Verification:**
    *   Execute SQL queries to test each registered UDF.
    *   Test the query rewriting by running `CREATE TABLE` and `SELECT` statements with `MYVECTOR` annotations.
    *   Test the binlog service by creating a table with an `online=Y` index, performing DML, and querying the index to verify it was updated.
5.  **Deactivation:** Run `UNINSTALL COMPONENT 'file://myvector'` and confirm that the component is cleanly unloaded and all UDFs are deregistered.

**Manual Step 5 (local):** Build the component (e.g. `./scripts/build-component.sh mysql-8.4.8 /path/to/mysql-server`). Copy `build/component/libmyvector_component.so` to your MySQL `plugin_dir` as `myvector.so` (e.g. `cp build/component/libmyvector_component.so $(mysql -N -e "SELECT @@plugin_dir;")/myvector.so`). Then connect and run `INSTALL COMPONENT 'file://myvector';`, verify UDFs (e.g. `SELECT myvector_display(myvector_construct('[1,2,3]'));`), then `UNINSTALL COMPONENT 'file://myvector';`.

### **Implementation Decisions**

*   **Configuration:** Remains dynamic via system variables; config file sets defaults.
*   **Binlog Positioning:** Persist and restore binlog position for correctness.
*   **Query Rewriting:** Backwards-compatibility for comments/whitespace variations is not required.
*   **Init Semantics:** Component `init` should fail if UDF registration or binlog startup fails to avoid partial activation.

### **Binlog Position Persistence Design**

*   **Storage Location:** Use a durable file under `myvector_index_dir` (e.g., `binlog_state.json`) to keep binlog file name, position, and server UUID.
*   **Write Policy:** Update the persisted position after each successful batch apply; fsync or atomic rename on write to avoid corruption.
*   **Recovery:** On start, validate the stored server UUID against the current server; if mismatched, refuse to start and log a clear error.
*   **Fallback:** If no state exists, start from a configured bootstrap position (or current master position) and log the choice explicitly.

### **Init Failure Handling**

*   **Order:** Load config defaults, register UDFs, then start binlog monitoring.
*   **Rollback:** If any step fails, undo previous steps (deregister UDFs, stop any started threads) and return a failing status from `init`.
*   **Logging:** Emit a single error summary plus detailed failure logs for each step to simplify diagnosis.

### **Overall Progress (2026-02-11)**

**Completed Work**

The initial scaffolding, creation of the core component file, and the implementation of the Query Rewriter, Binlog Monitoring, and UDF Registration services have
been completed. The `CMakeLists.txt` has also been updated to reflect the new component build structure.

**Current Blocker (resolved for Step 4)**

The build requires the MySQL **server** source tree (component headers are not in the client package). Step 4 is complete: `CMakeLists.txt` now requires `MYSQL_SOURCE_DIR` and finds `libmysqlclient`; the build should succeed when that variable is set to the server source root. Header discovery is therefore resolved; Step 8 (testing) is no longer blocked by it—remaining work is to run and expand tests (e.g., CI test-component matrix for 8.0/8.4/9.0).

**Next Steps**

Run the component build with `-DMYSQL_SOURCE_DIR=/path/to/mysql-server`, then run Step 5 (below). CI runs `build-component` and `test-component` (install + UDF smoke test + uninstall).

**Updated Todo List**

1.  [completed] Create a new directory for the component source code.
2.  [completed] Create the component manifest file.
3.  [completed] Create the main component source file.
4.  [completed] Implement the query rewriting service.
5.  [completed] Implement the binlog monitoring service.
6.  [completed] Implement the UDF registration.
7.  [completed] Update the build system to compile the component.
8.  [in_progress] Test the component.
