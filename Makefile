# Makefile for MyVector plugin

# Get compiler and linker flags from the installed MySQL server
MYSQL_CXX_FLAGS := $(shell mysql_config --cxxflags)
MYSQL_LDFLAGS := $(shell mysql_config --libs)

# Source files
SOURCES := $(wildcard src/*.cc)
OBJECTS := $(SOURCES:.cc=.o)

# Target shared library
TARGET := myvector.so

# Default target
all: $(TARGET)

$(TARGET): $(OBJECTS)
	$(CXX) $(MYSQL_CXX_FLAGS) -shared -o $@ $^ $(MYSQL_LDFLAGS) -I$(shell mysql_config --plugindir | sed 's/plugin/include/')

%.o: %.cc
	$(CXX) $(MYSQL_CXX_FLAGS) -fPIC -c -o $@ $<

clean:
	rm -f $(OBJECTS) $(TARGET)

.PHONY: all clean