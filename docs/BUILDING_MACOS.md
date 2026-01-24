# Building and testing MySQL on macOS (Apple Silicon)

This note captures build and test observations for recent MySQL releases
on macOS Apple Silicon. It is intended as a reference while building.

## Environment
- Hardware: Apple Silicon (arm64)
- Xcode: 26.2
- LLVM (Homebrew): 14–21

## Releases tested
- MySQL 8.0.45
- MySQL 8.4.8
- MySQL 9.6.0

## Build results
- All three versions compile with Xcode 26.2.
- Homebrew LLVM 14–17: CMake error on macOS (likely platform-related).
- LLVM 19/20: still fail to build 8.0.45 (bug #119238 / #119239).
- LLVM 19/20: 8.4.8 also affected by bug #119246 (regression).
- LLVM 21: still fails to build 9.6.0 (preexisting bug #119246).

## Notes
- The LLVM failures are pre-existing issues except the new 8.4.8 regression
  involving bug #119246.
