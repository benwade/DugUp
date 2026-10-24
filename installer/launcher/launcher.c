/* DugUp.exe: DugUp's own program file, so Windows shows "DugUp" (not "Python") in the Start menu,
 * Startup apps, the taskbar and Task Manager.
 *
 * It sits beside the bundled pythonw.exe and does exactly what pythonw.exe does: it loads the bundled
 * Python into this process and hands it the command line. So "DugUp.exe -m dugup tray" runs the tray, and
 * DugUp.exe can stand in for pythonw.exe anywhere. Started with nothing after it (a double-click), it opens
 * the DugUp window.
 *
 * Built fresh for every installer by installer/build.py, with the Microsoft C compiler; nothing of
 * Python's own files is changed. PYTHON_DLL names the bundled Python's DLL, such as L"python313.dll".
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>

#ifndef PYTHON_DLL
#error PYTHON_DLL must name the bundled Python's DLL
#endif

typedef int (*PyMainFunction)(int, wchar_t **);

static int fail(const wchar_t *what) {
    MessageBoxW(NULL, what, L"DugUp could not start", MB_OK | MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command_line, int show) {
    (void)instance; (void)previous; (void)command_line; (void)show;
    wchar_t path[32768];
    DWORD length = GetModuleFileNameW(NULL, path, (DWORD)(sizeof(path) / sizeof(path[0])));
    if (length == 0 || length >= sizeof(path) / sizeof(path[0]))
        return fail(L"DugUp could not find its own folder.");
    wchar_t *slash = wcsrchr(path, L'\\');
    if (slash == NULL)
        return fail(L"DugUp could not find its own folder.");
    slash[1] = L'\0';
    if (wcscat_s(path, sizeof(path) / sizeof(path[0]), PYTHON_DLL) != 0)
        return fail(L"DugUp's folder name is too long.");

    HMODULE python = LoadLibraryExW(path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (python == NULL)
        return fail(L"DugUp's own Python is missing or damaged. Installing DugUp again puts it back.");
    PyMainFunction py_main = (PyMainFunction)GetProcAddress(python, "Py_Main");
    if (py_main == NULL)
        return fail(L"DugUp's own Python is damaged. Installing DugUp again puts it back.");

    int count = 0;
    wchar_t **given = CommandLineToArgvW(GetCommandLineW(), &count);
    if (given == NULL)
        return fail(L"DugUp could not read its command line.");
    if (count <= 1) {                       /* a double-click: open the window */
        static wchar_t *window[5];
        window[0] = given[0];
        window[1] = L"-m";
        window[2] = L"dugup";
        window[3] = L"window";
        window[4] = NULL;
        return py_main(4, window);
    }
    return py_main(count, given);
}
