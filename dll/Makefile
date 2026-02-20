# Build dbvbot.dll with MinGW 32-bit
# Requires: i686 g++ (32-bit, matching dbvStart.exe)
#
# Usage:
#   make          — build dbvbot.dll
#   make clean    — remove build artifacts

CXX      = C:/mingw32/bin/g++
CXXFLAGS = -shared -static -s -O2 -std=c++17
LDFLAGS  = -lkernel32
TARGET   = dbvbot.dll
SRC      = dbvbot.cpp

all: $(TARGET)

$(TARGET): $(SRC)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGET)

.PHONY: all clean
