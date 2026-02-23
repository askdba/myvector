/**
 * Stub for standalone component build when MYSQL_BUILD_DIR is not set.
 * MySQL server generates this during its build; we define a minimal value
 * so component code compiles. Use 8.4.8 (80408) for version checks.
 */
#ifndef MYSQL_VERSION_ID
#define MYSQL_VERSION_ID 80408
#endif
