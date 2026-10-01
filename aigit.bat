@echo off
chcp 936 >nul 2>&1
rem 把控制台固定在 cp936:本文件是 GBK 编码的,若控制台在 UTF-8(chcp 65001)下,
rem 中文会被逐字节替换成一串问号。正常的中文 Windows 本来就是 936,这行是空操作;
rem 写这一行是为了在 Windows Terminal / 开了"全球语言支持 Unicode UTF-8"的机器上也不乱码。
rem 副作用:如果你是从自己的终端窗口里调用本文件的,那个窗口的代码页会变成 936。
rem ==========================================================================
rem  aigit 启动器(批处理版)
rem
rem  双击就能跑。做的事:
rem    1. 找 Python 3(先试 py -3,再试 python),找不到就给出能照着做的中文提示
rem    2. 找 git,找不到同样给中文提示
rem    3. 解析参数(-Repo / -Port / -Sandbox / -Force,其余原样透传给 server.py)
rem    4. 防护:仓库有未提交改动就拦下来;-Sandbox 指向真实仓库也拦下来
rem    5. 端口被占用就提前拦下来(不让用户看 Python 堆栈)
rem    6. 启动 server.py。**不自动开浏览器**,只打印 URL
rem
rem  编码:本文件是 GBK(cp936)。cmd 就是这个代码页,所以汉字能正常显示。
rem         不要再用 UTF-8 无 BOM 保存这个文件,cmd 按 GBK 读,汉字会变乱码。
rem
rem  用法:
rem    aigit.bat                                  用默认演练仓(系统临时目录\aigit-sandbox)
rem    aigit.bat -Sandbox                         重建演练仓(会先删掉同名目录)
rem    aigit.bat -Repo C:\work\myrepo             对着真实仓库开
rem    aigit.bat -Repo C:\work\myrepo -Force      明知有未提交改动,还是要开
rem    aigit.bat -Port 9000                       换端口
rem ==========================================================================

setlocal EnableExtensions EnableDelayedExpansion

set "AIGIT_DIR=%~dp0"

set "PORT=8777"
set "REPO="
set "SANDBOX="
set "FORCE="
set "PYEXE="
set "PASSTHRU="

rem 双击运行和从命令行运行,错误处理不一样:
rem   双击 -> 窗口会自己关掉,必须 pause 才看得见错误
rem   命令行 -> 窗口还在,pause 反而碍事
rem 用 %cmdcmdline% 区分:双击时它是  cmd /c ""路径\aigit.bat" "
set "INTERACTIVE=1"
echo !cmdcmdline! | findstr /I /C:"/c" >nul 2>&1 && set "INTERACTIVE="

rem ---------------------------------------------------------------- 参数解析
:parse_args
if "%~1"=="" goto :args_done
set "ARG=%~1"

if /i "%ARG%"=="-Repo" goto :need_value_repo
if /i "%ARG%"=="/Repo" goto :need_value_repo
if /i "%ARG%"=="--repo" goto :need_value_repo
if /i "%ARG%"=="-Port" goto :need_value_port
if /i "%ARG%"=="/Port" goto :need_value_port
if /i "%ARG%"=="--port" goto :need_value_port
if /i "%ARG%"=="-Sandbox" goto :flag_sandbox
if /i "%ARG%"=="/Sandbox" goto :flag_sandbox
if /i "%ARG%"=="--sandbox" goto :flag_sandbox
if /i "%ARG%"=="-Force" goto :flag_force
if /i "%ARG%"=="/Force" goto :flag_force
if /i "%ARG%"=="--force" goto :flag_force
if /i "%ARG%"=="-h" goto :usage
if /i "%ARG%"=="/h" goto :usage
if /i "%ARG%"=="--help" goto :usage
if /i "%ARG%"=="--" goto :pass_rest
goto :passthru_one

:need_value_repo
set "FLAGNAME=%~1"
shift
if "%~1"=="" goto :missing_value
set "REPO=%~1"
goto :next_arg

:need_value_port
set "FLAGNAME=%~1"
shift
if "%~1"=="" goto :missing_value
set "PORT=%~1"
goto :next_arg

:flag_sandbox
set "SANDBOX=1"
goto :next_arg

:flag_force
set "FORCE=1"
goto :next_arg

:usage
call :print_usage
endlocal
exit /b 0

:pass_rest
shift
:passthru_loop
if "%~1"=="" goto :args_done
set "PASSTHRU=!PASSTHRU! "%~1""
shift
goto :passthru_loop

:passthru_one
set "PASSTHRU=!PASSTHRU! "%ARG%""
:next_arg
shift
goto :parse_args

:args_done

rem ---------------------------------------------------------------- 默认仓库
if not defined REPO (
    if defined TEMP (
        set "REPO=%TEMP%\aigit-sandbox"
    ) else if defined TMP (
        set "REPO=%TMP%\aigit-sandbox"
    ) else (
        set "REPO=%CD%\aigit-sandbox"
    )
)

rem 路径里不许有双引号,否则下面所有 "%REPO%" 都会被拆坏
if not defined REPO goto :bad_repo
set "REPO_CHECK=!REPO:"="!"
if not "!REPO!"=="!REPO_CHECK!" goto :bad_repo

rem ---------------------------------------------------------------- 端口校验
rem 顺序要紧:先确认非空、再确认全是数字、再去掉前导零、最后比范围。
rem 用 for /f 判「全是数字」而不是 echo 出去再 findstr,是因为后者会把
rem 用户输入直接拼进命令行,一个 1 & calc 就能执行任意命令。
if not defined PORT goto :bad_port
for /f "delims=0123456789" %%A in ("!PORT!") do goto :bad_port
:strip_zeros
if "!PORT:~0,1!"=="0" if not "!PORT!"=="0" (
    set "PORT=!PORT:~1!"
    goto :strip_zeros
)
if !PORT! LSS 1 goto :bad_port
if !PORT! GTR 65535 goto :bad_port

if not exist "%AIGIT_DIR%server.py" (
    call :die "找不到 server.py" ^
        "应该在 %AIGIT_DIR%server.py" ^
        "但这个文件不存在。" ^
        "请确认 aigit.bat 和 server.py 在同一个文件夹里。"
    goto :fail
)

rem ---------------------------------------------------------------- 找 Python
call :find_python
if not defined PYEXE (
    call :die_python
    goto :fail
)

rem ---------------------------------------------------------------- 找 git
git --version >nul 2>&1
if errorlevel 1 (
    call :die "找不到 git 命令" ^
        "aigit 要在真实的 git 仓库上工作,没有 git 跑不了。" ^
        "装法:到 https://git-scm.com/download/win 下载 Git for Windows," ^
        "一路下一步装完。安装程序会问要不要改 PATH,选 Yes。" ^
        "装完要新开一个命令行窗口,再双击本文件(env 不会自动刷新)。"
    goto :fail
)

rem ---------------------------------------------------------------- 沙箱防护
if defined SANDBOX goto :sandbox_guard

rem 普通模式:仓库里有未提交的改动就拦下来。
rem 理由:界面上的「丢弃改动」会真删文件,在真实仓库上点错一下就找不回来了。
if exist "%REPO%\.git" (
    set "DIRTY=0"
    for /f "usebackq delims=" %%L in (`git -C "%REPO%" status --porcelain -uno 2^>nul`) do set "DIRTY=!DIRTY!+1"
    set /a DIRTY=!DIRTY! 2>nul
    if !DIRTY! GTR 0 goto :dirty_block
    set "UNTRACKED=0"
    for /f "usebackq delims=" %%L in (`git -C "%REPO%" status --porcelain 2^>nul`) do set "UNTRACKED=!UNTRACKED!+1"
    set /a UNTRACKED=!UNTRACKED! 2>nul
    if !UNTRACKED! GTR 0 (
        echo [提醒] 仓库里有 !UNTRACKED! 个未跟踪文件。
        echo        「丢弃改动」对未跟踪文件是**直接删除文件**,不是恢复到上一版。
        echo        确认这些文件都不重要再继续。
        echo.
    )
    goto :guards_done
)
if exist "%REPO%" (
    if not exist "%REPO%\.git" (
        call :die "目标目录不是 git 仓库" ^
            "「%REPO%」存在,但里面没有 .git。" ^
            "两条路:一是在这个目录跑 git init;二是换个已经是仓库的目录," ^
            "用 aigit.bat -Repo 加路径 指定。" ^
            "只想试手感和造测试数据的话,直接双击本文件用默认的演练仓就好。"
        goto :fail
    )
)
goto :guards_done

:dirty_block
if defined FORCE goto :dirty_warn
call :die "仓库里有 !DIRTY! 处未提交的改动" ^
    "「%REPO%」有 !DIRTY! 处已跟踪文件被改过还没提交。" ^
    "aigit 界面上的「丢弃改动」会真的改工作区文件,未提交的改动一旦点掉就没了。" ^
    "三条路:" ^
    "  1) 先自己处理掉:git status 看清是什么,git stash 或 git commit 都行。" ^
    "  2) 换个干净的演练仓:aigit.bat -Sandbox 会新建一个,随便删。" ^
    "  3) 你确认过这些改动不要了,加 -Force 强行开:" ^
    "     aigit.bat -Repo 该路径 -Force" ^
    "本工具的交付说明里,这一条是硬性建议:不要在有未提交重要改动的真实仓库上跑。"
goto :fail

:dirty_warn
echo [提醒] -Force 已指定,忽略上面的 !DIRTY! 处未提交改动,继续启动。
echo        「丢弃改动」点下去是真的删文件,自己把握。
echo.

:sandbox_guard
rem 沙箱模式:server.py 会 shutil.rmtree 掉整个目录。
rem 先确认这不是一个连着远端的真实仓库。
if exist "%REPO%\.git" (
    set "REMOTE="
    for /f "usebackq delims=" %%L in (`git -C "%REPO%" remote 2^>nul`) do set "REMOTE=%%L"
    if defined REMOTE goto :sandbox_block
)
if not defined FORCE (
    echo [提醒] -Sandbox 会先把「%REPO%」整个删掉,再建一个新的演练仓。
    echo        这个目录现在不存在,建好后随便删。
    echo.
)
goto :guards_done

:sandbox_block
if defined FORCE goto :sandbox_warn
call :die "-Sandbox 会删掉整个目录,而「%REPO%」配了远端" ^
    "配了 remote 的目录是真仓库,不是演练仓,已经拦下来。" ^
    "两条路:" ^
    "  1) 换个路径当演练仓:aigit.bat -Sandbox -Repo 系统临时目录\aigit-demo" ^
    "  2) 你确实要拿它当一次性演练仓(远端配置会一起没),加 -Force:" ^
    "     aigit.bat -Sandbox -Repo 该路径 -Force"
goto :fail

:sandbox_warn
echo [提醒] -Force 已指定,仍然会把「%REPO%」整个删掉(连远端配置一起)。
echo.

:guards_done

rem ---------------------------------------------------------------- 端口占用
rem 在启动之前就查掉。原因是 server.py 撞上端口时会直接抛
rem OSError: [WinError 10048] 的 Python 堆栈,对使用者毫无意义。
call :netstat_listening %PORT% LISTENED
if "%LISTENED%"=="1" (
    call :die "端口 %PORT% 已经被别的程序占用了" ^
        "aigit 起不来,因为没人能同时听同一个端口。" ^
        "查到是谁占着:netstat -ano 然后 findstr :%PORT%    (最后一列是 PID)" ^
        "换端口:aigit.bat -Port 8888" ^
        "或者关掉占用它的那个程序再重试。" ^
        "注意:占用的可能是上一次没退干净的 aigit,那就不用换端口,直接重开就行。"
    goto :fail
)

rem ---------------------------------------------------------------- 启动
set "SANDBOXARG="
if defined SANDBOX set "SANDBOXARG=--sandbox"

echo.
echo ------------------------------------------------------------
echo   aigit
echo   python    !PYEXE!
echo   仓库      "%REPO%"
if defined SANDBOX echo   模式      演练仓(已清空重建)
if not defined SANDBOX echo   模式      现有仓库(改动请当心)
echo   打开      http://127.0.0.1:%PORT%/
echo   停止      在这个窗口按 Ctrl+C
echo   提醒      不会自动打开浏览器,请自己复制上面那行地址
echo ------------------------------------------------------------
echo.

%PYEXE% "%AIGIT_DIR%server.py" --root "%REPO%" --port %PORT% %SANDBOXARG% %PASSTHRU%
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" goto :done
echo.
echo [错误] server.py 以退出码 %RC% 结束。
if "%RC%"=="1" (
    echo        上面若有 "Address already in use" 或 "WinError 10048",说明端口又被占了,
    echo        换个端口重试:aigit.bat -Port 8888
    echo        端口检查是在启动前做的,这一步是防「检查完到启动之间」被抢走的极少情况。
) else (
    echo        把上面几行报错连同这个窗口的内容一起反馈即可。
)
echo        aigit.bat 自己只认这五个参数:
echo          -Repo 路径   -Port 端口   -Sandbox   -Force   -h
echo        其余参数是原样透传给 server.py 的。所以看到
echo        "unrecognized arguments" 基本就是参数名拼错了。
echo.
goto :fail

:done
echo.
echo aigit 已停止。
endlocal
exit /b 0

rem ================================================================= 子程序

rem -- 找 Python 3 -------------------------------------------------------------
:find_python
set "PYEXE="
rem 优先 py -3(py launcher 能明确指定 3.x,不会撞上 Python 2 的 python.exe)
py -3 -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py -3"
    exit /b 0
)
rem 再试 PATH 里的 python
python -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=python"
    exit /b 0
)
rem 有些机器上装的是 python3
python3 -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=python3"
    exit /b 0
)
exit /b 1

rem -- 端口是否有人在听 ---------------------------------------------------------
rem 用法:netstat_listening <端口> <结果变量>
rem 结果变量为 LISTENED 表示有进程在 LISTENING。
rem 为什么用 netstat 而不是直接起一个进程去试:这是最省的一招,不用 fork 额外进程。
:netstat_listening
set "%~2=0"
for /f "usebackq tokens=1" %%P in (`netstat -ano -p TCP 2^>nul ^| findstr /R /C:":%~1 .*LISTENING"`) do set "%~2=1"
exit /b 0

rem -- 统一的错误出口 ----------------------------------------------------------
rem 用法:die <标题> <行1> <行2> ...
rem 注意:这里不能写 %TEMP% 这类东西。call 会做第二次展开,%TEMP% 会被
rem 替换成真实路径,提示文字就变成机器相关的了。要写就写「系统临时目录」。
:die
set "TITLE=%~1"
shift
echo.
echo ============================================================
echo   [启动失败] %TITLE%
echo ============================================================
:die_loop
if "%~1"=="" goto :die_end
echo   %~1
shift
goto :die_loop
:die_end
echo.
echo   上面这些不是崩溃,是启动前的检查没通过,按提示改完再双击本文件即可。
echo.
if defined INTERACTIVE pause
exit /b 1

rem -- 没有 Python 的专门提示 -------------------------------------------------
:die_python
set "HAVE_PY="
py --version >nul 2>&1
if not errorlevel 1 set "HAVE_PY=1"
set "HAVE_PYTHON="
where python >nul 2>&1
if not errorlevel 1 set "HAVE_PYTHON=1"

echo.
echo ============================================================
echo   [启动失败] 没找到可用的 Python 3
echo ============================================================
echo.
if defined HAVE_PY (
    echo   找到了 py 启动器,但它下面没有装 Python 3。
    echo   多半是只装了 Python 2,或者装 Python 3 时选了别的选项。
) else (
    echo   这台机器上既没有 py 启动器,PATH 里也没有 python。
)
if defined HAVE_PYTHON (
    echo   另外:PATH 里是有一个 python,但它不是 Python 3,或者根本跑不起来。
)
echo.
echo   照着做,三步:
echo     1. 浏览器打开 https://www.python.org/downloads/windows/
echo     2. 下载 "Windows installer" 那个 64-bit 版本,推荐 3.11 或更新的 3.x。
echo        安装向导第一页务必勾上:
echo          [x] Add python.exe to PATH
echo        这一项默认是**不勾**的。不勾的话,双击本文件永远找不到 python。
echo     3. 一路 "Next" 装完,然后**新开一个命令行窗口**,
echo        再双击本文件。
echo.
echo   装完想确认:在命令行里敲 python --version,应该显示 Python 3.x.x
echo   如果公司网络下不了,可以让 IT 装一个免安装版,也就是 python embeddable package,
echo   然后本文件用 aigit.bat -Python 加完整路径 指定它。
echo.
echo   上面这些不是崩溃,是启动前的检查没通过,按提示改完再双击本文件即可。
echo.
if defined INTERACTIVE pause
exit /b 1

rem -- 用法 --------------------------------------------------------------------
:print_usage
echo aigit - 本地 git 图形工具启动器(批处理版)
echo.
echo   aigit.bat [-Repo 路径] [-Port 端口] [-Sandbox] [-Force] [-h]
echo.
echo   -Repo 路径   要打开的仓库。默认是系统临时目录下的 aigit-sandbox
echo   -Port 端口   监听端口。默认 8777,冲突时换一个
echo   -Sandbox     把 -Repo 指定的目录整个删掉,重建一个干净的演练仓。
echo                 会先检查那是不是配了远端的真仓库,是就拦下来
echo   -Force       跳过「有未提交改动」和「-Sandbox 指向带远端的仓库」两道拦截
echo   -h           显示本帮助
echo.
echo   其余参数原样透传给 server.py。
echo.
echo   例:
echo     aigit.bat -Sandbox
echo     aigit.bat -Repo C:\work\myrepo -Port 9000
echo   注意:不会自动打开浏览器,启动后自己复制窗口里那行 http 地址。
exit /b 0

rem ================================================================= 错误出口

:missing_value
call :die "参数缺值" ^
    "「%FLAGNAME%」后面要跟一个值。" ^
    "-Repo 后面跟仓库路径,-Port 后面跟端口号。" ^
    "看用法:aigit.bat -h"
goto :fail


:bad_port
call :die "端口号不合法:「%PORT%」" ^
    "端口必须是 1 到 65535 之间的整数。" ^
    "正确写法:aigit.bat -Port 9000" ^
    "注意:写 -Port 9000,不是 -Port:9000,也不要带前导零。"
goto :fail

:bad_repo
call :die "仓库路径不合法:「%REPO%」" ^
    "路径里不能带双引号,否则命令行会被拆坏。" ^
    "换个不带引号的目录,或者用 -Sandbox 建一个演练仓。"
goto :fail

:fail
endlocal
exit /b 1
