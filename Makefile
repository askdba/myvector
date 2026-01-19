# Makefile for MyVector plugin

# Get compiler and linker flags from the installed MySQL server
MYSQL_CXX_FLAGS ?= $(shell mysql_config --cxxflags 2>/dev/null || echo "-I/usr/include/mysql")
MYSQL_LDFLAGS ?= $(shell mysql_config --libs 2>/dev/null || echo "-lmysqlclient")

# Add local include directory for myvector headers
INCLUDE_FLAGS := -I./include

# Source files
SOURCES := $(wildcard src/*.cc)
OBJECTS := $(SOURCES:.cc=.o)

# Target shared library
TARGET := myvector.so

# Default target
all: $(TARGET)

$(TARGET): $(OBJECTS)
	$(CXX) $(MYSQL_CXX_FLAGS) $(INCLUDE_FLAGS) -shared -o $@ $^ $(MYSQL_LDFLAGS)

%.o: %.cc
	$(CXX) $(MYSQL_CXX_FLAGS) $(INCLUDE_FLAGS) -fPIC -c -o $@ $<

clean:
	rm -f $(OBJECTS) $(TARGET)

.PHONY: all clean