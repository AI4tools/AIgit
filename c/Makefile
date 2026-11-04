# aigit C 版（M0 骨架）—— MinGW-w64，零第三方依赖，只链 Win32 + Winsock2
#
#   mingw32-make -C c -j4          -> c/build/aigit.exe
#   mingw32-make -C c clean
#
# 产物是**静态**单文件：-static -static-libgcc。不依赖 Python。

ifeq ($(origin CC),default)
CC := gcc
endif
CFLAGS  ?= -std=c11 -O2 -g -Wall -Wextra -Wno-unused-parameter -D_WIN32_WINNT=0x0601 -DUNICODE -D_UNICODE -D__USE_MINGW_ANSI_STDIO=1
LDFLAGS ?= -static -static-libgcc -municode
LIBS    := -lws2_32
SRC     := $(wildcard src/*.c)
OBJ     := $(SRC:src/%.c=build/%.o)

all: build/aigit.exe

build/aigit.exe: $(OBJ)
	$(CC) $(LDFLAGS) -o $@ $(OBJ) $(LIBS)

build/%.o: src/%.c src/*.h | build
	$(CC) $(CFLAGS) -c -o $@ $<

# mkdir / rm / rmdir 各写一条，前面加 '-' 让另一种 shell 下的失败被忽略：
# cmd 下用 mkdir + rmdir，MSYS2/bash 下用 mkdir + rm。
build:
	-@mkdir build
	-@rm -rf build

clean:
	-@rm -rf build
	-@rmdir /s /q build

.PHONY: all clean
