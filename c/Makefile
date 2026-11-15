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

# 同一份 makefile 要同时喂给 cmd（本地 mingw32-make）和 sh（MSYS2 / CI）：
#   * 建目录只有一句 mkdir（两个 shell 都认）；
#   * 删目录两种写法都列上，前面加 '-' 让另一种 shell 下的失败被忽略。
# 注意：build 目标里**不能**有 rm —— 以前误写了一行，cmd 下 rm 不存在所以
# 本地一直绿，MSYS2 的 sh 下会把刚建好的 build 目录删掉，CI 会全挂在编译前。
build:
	-@mkdir build

clean:
	-@rm -rf build
	-@rmdir /s /q build

.PHONY: all clean
