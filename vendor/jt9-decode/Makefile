# Makefile for jt9_decode

CXX = g++
MOC = moc
TARGET = jt9_decode
SOURCE = jt9_decode.cpp
MOC_SOURCE = jt9_decode.moc
INCLUDES = -I./wsjtx
CXXFLAGS = -std=c++11 -fPIC $(shell pkg-config --cflags Qt5Core)
LDFLAGS = $(shell pkg-config --libs Qt5Core) -lrt

.PHONY: all clean

all: $(TARGET)

# Generate MOC file from source (Qt meta-object compiler)
$(MOC_SOURCE): $(SOURCE)
	$(MOC) $(SOURCE) -o $(MOC_SOURCE)

# Build target (MOC file is included in source via #include)
$(TARGET): $(SOURCE) $(MOC_SOURCE)
	$(CXX) -o $(TARGET) $(SOURCE) $(INCLUDES) $(CXXFLAGS) $(LDFLAGS)

clean:
	rm -f $(TARGET) $(MOC_SOURCE)

install: $(TARGET)
	install -m 755 $(TARGET) /usr/local/bin/

uninstall:
	rm -f /usr/local/bin/$(TARGET)
