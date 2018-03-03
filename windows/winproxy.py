import ctypes
import functools

import windows

from ctypes.wintypes import *
from windows.generated_def.winstructs import *
from windows.generated_def.windef import *
import windows.generated_def.winfuncs as winfuncs
from windows.generated_def.ntstatus import NtStatusException
from windows.dbgprint import dbgprint

## winfuncs helpers

def is_implemented(winfunc):
    try:
        winfunc.force_resolution()
    except ExportNotFound:
        return False
    return True


def get_target(winfunc):
    """POC for new hook"""
    return winfunc.target_dll, winfunc.target_func


# Rip-of windows.utils: should be removed from the other place ?
def _get_func_addr(dll_name, func_name):
        # Load the DLL
        ctypes.WinDLL(dll_name)
        modules = windows.current_process.peb.modules
        if not dll_name.lower().endswith(".dll"):
            dll_name += ".dll"
        mod = [x for x in modules if x.name == dll_name][0]
        return mod.pe.exports[func_name]


def resolve(winfunc):
    winfunc.force_resolution()
    return _get_func_addr(*get_target(winfunc))


class Kernel32Error(WindowsError):
    def __new__(cls, func_name):
        win_error = ctypes.WinError()
        api_error = super(Kernel32Error, cls).__new__(cls)
        api_error.api_name = func_name
        api_error.winerror = win_error.winerror
        api_error.strerror = win_error.strerror
        api_error.args = (func_name, win_error.winerror, win_error.strerror)
        return api_error

    def __repr__(self):
        return "{0}: {1}".format(self.api_name, super(Kernel32Error, self).__repr__())

    def __str__(self):
        return "{0}: {1}".format(self.api_name, super(Kernel32Error, self).__str__())


class IphlpapiError(Kernel32Error):

    def __new__(cls, func_name, code, strerror=None):
        win_error = ctypes.WinError(code)
        api_error = super(Kernel32Error, cls).__new__(cls)
        api_error.api_name = func_name
        api_error.winerror = win_error.winerror
        if strerror is not None:
            api_error.strerror = strerror
        else:
            api_error.strerror = win_error.strerror
        api_error.args = (func_name, api_error.winerror, api_error.strerror)
        return api_error

    def __init__(self, func_name, code, strerror=None):
        pass


# Error check method
def no_error_check(func_name, result, func, args):
    """Nothing special"""
    return args


def minus_one_error_check(func_name, result, func, args):
    if result == -1:
        raise Kernel32Error(func_name)
    return args


def zero_is_fail_error_check(func_name, result, func, args):
    """raise Kernel32Error if result is 0"""
    if not result:
        raise Kernel32Error(func_name)
    return args


def should_return_zero_check(func_name, result, func, args):
    """raise Kernel32Error if result is NOT 0"""
    if result:
        raise Kernel32Error(func_name)
    return args


def iphlpapi_error_check(func_name, result, func, args):
    """raise IphlpapiError if result is NOT 0"""
    if result:
        raise IphlpapiError(func_name, result)
    return args


def error_ntstatus(func_name, result, func, args):
    if result:
        raise NtStatusException(result & 0xffffffff)
    return args

def valid_handle_check(func_name, result, func, args):
    if result == INVALID_HANDLE_VALUE:
        raise Kernel32Error(func_name)
    return args


class ExportNotFound(RuntimeError):
        def __init__(self, func_name, api_name):
            self.func_name = func_name
            self.api_name = api_name
            super(ExportNotFound, self).__init__("Function {0} not found into {1}".format(func_name, api_name))


class ApiProxy(object):
    APIDLL = None
    """Create a python wrapper around a kernel32 function"""
    def __init__(self, func_name, error_check=None, deffunc_module=None):
        self.deffunc_module = deffunc_module if deffunc_module is not None else winfuncs
        self.func_name = func_name
        if error_check is None:
            error_check = self.default_error_check
        self.error_check = functools.wraps(error_check)(functools.partial(error_check, func_name))
        self._cprototyped = None

    def __call__(self, python_proxy, ):
        prototype = getattr(self.deffunc_module, self.func_name + "Prototype")
        params = getattr(self.deffunc_module, self.func_name + "Params")
        python_proxy.prototype = prototype
        python_proxy.params = params
        python_proxy.errcheck = self.error_check
        python_proxy.target_dll = self.APIDLL
        python_proxy.target_func = self.func_name
        params_name = [param[1] for param in params]
        if (self.error_check.__doc__):
            doc = python_proxy.__doc__
            doc = doc if doc else ""
            python_proxy.__doc__ = doc + "\nErrcheck:\n   " + self.error_check.__doc__


        def generate_ctypes_function():
            try:
                c_prototyped = prototype((self.func_name, getattr(ctypes.windll, self.APIDLL)), params)
            except (AttributeError, WindowsError):
                raise ExportNotFound(self.func_name, self.APIDLL)
            c_prototyped.errcheck = self.error_check
            self._cprototyped = c_prototyped


        def perform_call(*args):
            if len(params_name) != len(args):
                print("ERROR:")
                print("Expected params: {0}".format(params_name))
                print("Just Got params: {0}".format(args))
                raise ValueError("I do not have all parameters: how is that possible ?")
            for param_name, param_value in zip(params_name, args):
                if param_value is NeededParameter:
                    raise TypeError("{0}: Missing Mandatory parameter <{1}>".format(self.func_name, param_name))
            if self._cprototyped is None:
                generate_ctypes_function()
            return self._cprototyped(*args)

        setattr(python_proxy, "ctypes_function", perform_call)
        setattr(python_proxy, "force_resolution", generate_ctypes_function)
        return python_proxy


class Kernel32Proxy(ApiProxy):
    APIDLL = "kernel32"
    default_error_check = staticmethod(zero_is_fail_error_check)


class Advapi32Proxy(ApiProxy):
    APIDLL = "advapi32"
    default_error_check = staticmethod(zero_is_fail_error_check)


class IphlpapiProxy(ApiProxy):
    APIDLL = "iphlpapi"
    default_error_check = staticmethod(iphlpapi_error_check)

class NtdllProxy(ApiProxy):
    APIDLL = "ntdll"
    default_error_check = staticmethod(should_return_zero_check)

class WinTrustProxy(ApiProxy):
    APIDLL = "wintrust"
    default_error_check = staticmethod(no_error_check)

class Ole32Proxy(ApiProxy):
    APIDLL = "ole32"
    default_error_check = staticmethod(no_error_check)

class PsapiProxy(ApiProxy):
    APIDLL = "psapi"
    default_error_check = staticmethod(zero_is_fail_error_check)

class User32Proxy(ApiProxy):
    APIDLL = "user32"
    default_error_check = staticmethod(zero_is_fail_error_check)

class VersionProxy(ApiProxy):
    APIDLL = "version"
    default_error_check = staticmethod(zero_is_fail_error_check)

class Crypt32Proxy(ApiProxy):
    APIDLL = "crypt32"
    default_error_check = staticmethod(zero_is_fail_error_check)


class CryptUIProxy(ApiProxy):
    APIDLL = "cryptui"
    default_error_check = staticmethod(zero_is_fail_error_check)

class Shell32Proxy(ApiProxy):
    APIDLL = "shell32"
    default_error_check = staticmethod(zero_is_fail_error_check)

class Ktmw32Proxy(ApiProxy):
    APIDLL = "Ktmw32"
    default_error_check = staticmethod(zero_is_fail_error_check)

#class OptionalExport(object):
#    """used 'around' a Proxy decorator
#       Should be used for export that are not available everywhere (ntdll internals | 32/64 bits stuff)
#       If the export is not found the function will be None
#
#       Example:
#            @OptionalExport(NtdllProxy('NtWow64ReadVirtualMemory64'))
#            def NtWow64ReadVirtualMemory64(...)
#            ...
#    """
#    def __init__(self, subdecorator):
#        self.subdecorator = subdecorator
#
#    def __call__(self, f):
#        try:
#            x = self.subdecorator(f)
#            x.force_resolution()
#            return x
#        except ExportNotFound as e:
#            dbgprint("Export <{e.func_name}> not found in <{e.api_name}>".format(e=e), "EXPORTNOTFOUND")
#            return None

class TransparentApiProxy(object):
    def __init__(self, DLLNAME, target_func, error_check):
        self.target_dll = DLLNAME
        self.target_func = target_func
        self.error_check = error_check
        self._ctypes_function = None

        self.prototype = getattr(winfuncs, target_func + "Prototype")
        self.params = getattr(winfuncs, target_func + "Params")

    def __call__(self, *args, **kwargs):
        if self._ctypes_function is None:
            self.force_resolution()
        return self._ctypes_function(*args, **kwargs)

    def force_resolution(self):
        try:
            c_prototyped = self.prototype((self.target_func, getattr(ctypes.windll, self.target_dll)), self.params)
        except AttributeError:
            raise ExportNotFound(self.target_func, self.target_dll)
        c_prototyped.errcheck = functools.wraps(self.error_check)(functools.partial(self.error_check, self.target_func))
        self._ctypes_function = c_prototyped


TransparentKernel32Proxy = lambda func_name, error_check=zero_is_fail_error_check: TransparentApiProxy("kernel32", func_name, error_check)
TransparentUser32Proxy = lambda func_name, error_check=zero_is_fail_error_check: TransparentApiProxy("user32", func_name, error_check)
TransparentAdvapi32Proxy = lambda func_name, error_check=zero_is_fail_error_check: TransparentApiProxy("advapi32", func_name, error_check)
TransparentIphlpapiProxy = lambda func_name, error_check=iphlpapi_error_check: TransparentApiProxy("iphlpapi", func_name, error_check)


class NeededParameterType(object):
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super(NeededParameterType, cls).__new__(cls)
        return cls._inst

    def __repr__(self):
        return "NeededParameter"
NeededParameter = NeededParameterType()

ExitProcess = TransparentKernel32Proxy("ExitProcess")
TerminateProcess = TransparentKernel32Proxy("TerminateProcess")
CloseHandle = TransparentKernel32Proxy("CloseHandle")
GetProcAddress = TransparentKernel32Proxy("GetProcAddress")
LoadLibraryA = TransparentKernel32Proxy("LoadLibraryA")
LoadLibraryW = TransparentKernel32Proxy("LoadLibraryW")
GetLastError = TransparentKernel32Proxy("GetLastError", no_error_check)
GetCurrentProcess = TransparentKernel32Proxy("GetCurrentProcess")
GetCurrentProcessorNumber = TransparentKernel32Proxy("GetCurrentProcessorNumber", no_error_check)
GetCurrentThread = TransparentKernel32Proxy("GetCurrentThread")
AllocConsole = TransparentKernel32Proxy("AllocConsole")
FreeConsole = TransparentKernel32Proxy("FreeConsole")
GetStdHandle = TransparentKernel32Proxy("GetStdHandle")
SetStdHandle = TransparentKernel32Proxy("SetStdHandle")
GetCurrentThreadId = TransparentKernel32Proxy("GetCurrentThreadId")
TerminateThread = TransparentKernel32Proxy("TerminateThread")
ExitThread = TransparentKernel32Proxy("ExitThread")
SuspendThread = TransparentKernel32Proxy("SuspendThread", minus_one_error_check)
ResumeThread = TransparentKernel32Proxy("ResumeThread", minus_one_error_check)
GetThreadId = TransparentKernel32Proxy("GetThreadId")
VirtualQueryEx = TransparentKernel32Proxy("VirtualQueryEx")
GetExitCodeThread = TransparentKernel32Proxy("GetExitCodeThread")
GetExitCodeProcess = TransparentKernel32Proxy("GetExitCodeProcess")
GetProcessId = TransparentKernel32Proxy("GetProcessId")
lstrcmpA = TransparentKernel32Proxy("lstrcmpA")
lstrcmpW = TransparentKernel32Proxy("lstrcmpW")
GetVersionExA = TransparentKernel32Proxy("GetVersionExA")
GetVersionExW = TransparentKernel32Proxy("GetVersionExW")
GetComputerNameA = TransparentKernel32Proxy("GetComputerNameA")
GetComputerNameW = TransparentKernel32Proxy("GetComputerNameW")
LocalFree = TransparentKernel32Proxy("LocalFree", should_return_zero_check)



Wow64DisableWow64FsRedirection = TransparentKernel32Proxy("Wow64DisableWow64FsRedirection")
Wow64RevertWow64FsRedirection = TransparentKernel32Proxy("Wow64RevertWow64FsRedirection")
Wow64EnableWow64FsRedirection = TransparentKernel32Proxy("Wow64EnableWow64FsRedirection")
Wow64GetThreadContext = TransparentKernel32Proxy("Wow64GetThreadContext")


def CreateFile_error_check(func_name, result, func, args):
    """raise Kernel32Error if result is NOT 0"""
    if result == INVALID_HANDLE_VALUE:
        raise Kernel32Error(func_name)
    return args


@Kernel32Proxy("CreateFileA", error_check=CreateFile_error_check)
def CreateFileA(lpFileName, dwDesiredAccess=GENERIC_READ, dwShareMode=0, lpSecurityAttributes=None, dwCreationDisposition=OPEN_EXISTING, dwFlagsAndAttributes=FILE_ATTRIBUTE_NORMAL, hTemplateFile=None):
    return CreateFileA.ctypes_function(lpFileName, dwDesiredAccess, dwShareMode, lpSecurityAttributes, dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile)


@Kernel32Proxy("CreateFileW", error_check=CreateFile_error_check)
def CreateFileW(lpFileName, dwDesiredAccess=GENERIC_READ, dwShareMode=0, lpSecurityAttributes=None, dwCreationDisposition=OPEN_EXISTING, dwFlagsAndAttributes=FILE_ATTRIBUTE_NORMAL, hTemplateFile=None):
    return CreateFileA.ctypes_function(lpFileName, dwDesiredAccess, dwShareMode, lpSecurityAttributes, dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile)


@Kernel32Proxy("VirtualAlloc")
def VirtualAlloc(lpAddress=0, dwSize=NeededParameter, flAllocationType=MEM_COMMIT, flProtect=PAGE_EXECUTE_READWRITE):
    return VirtualAlloc.ctypes_function(lpAddress, dwSize, flAllocationType, flProtect)


@Kernel32Proxy("VirtualFree")
def VirtualFree(lpAddress, dwSize=0, dwFreeType=MEM_RELEASE):
    return VirtualFree.ctypes_function(lpAddress, dwSize, dwFreeType)


@Kernel32Proxy("VirtualAllocEx")
def VirtualAllocEx(hProcess, lpAddress=0, dwSize=NeededParameter, flAllocationType=MEM_COMMIT, flProtect=PAGE_EXECUTE_READWRITE):
    return VirtualAllocEx.ctypes_function(hProcess, lpAddress, dwSize, flAllocationType, flProtect)


@Kernel32Proxy("VirtualFreeEx")
def VirtualFreeEx(hProcess, lpAddress, dwSize=0, dwFreeType=MEM_RELEASE):
    return VirtualFreeEx.ctypes_function(hProcess, lpAddress, dwSize, dwFreeType)


@Kernel32Proxy("CreateThread")
def CreateThread(lpThreadAttributes=None, dwStackSize=0, lpStartAddress=NeededParameter, lpParameter=NeededParameter, dwCreationFlags=0, lpThreadId=None):
    return CreateThread.ctypes_function(lpThreadAttributes, dwStackSize, lpStartAddress, lpParameter, dwCreationFlags, lpThreadId)


@Kernel32Proxy("CreateRemoteThread")
def CreateRemoteThread(hProcess=NeededParameter, lpThreadAttributes=None, dwStackSize=0,
                       lpStartAddress=NeededParameter, lpParameter=NeededParameter, dwCreationFlags=0, lpThreadId=None):
    return CreateRemoteThread.ctypes_function(hProcess, lpThreadAttributes, dwStackSize, lpStartAddress, lpParameter, dwCreationFlags, lpThreadId)


@Kernel32Proxy("VirtualProtect")
def VirtualProtect(lpAddress, dwSize, flNewProtect, lpflOldProtect=None):
    if lpflOldProtect is None:
        lpflOldProtect = ctypes.byref(DWORD())
    return VirtualProtect.ctypes_function(lpAddress, dwSize, flNewProtect, lpflOldProtect)


@Kernel32Proxy("VirtualProtectEx")
def VirtualProtectEx(hProcess, lpAddress, dwSize, flNewProtect, lpflOldProtect=None):
    if lpflOldProtect is None:
        lpflOldProtect = ctypes.byref(DWORD())
    return VirtualProtectEx.ctypes_function(hProcess, lpAddress, dwSize, flNewProtect, lpflOldProtect)

@Kernel32Proxy("CreateProcessA")
def CreateProcessA(lpApplicationName, lpCommandLine=None, lpProcessAttributes=None, lpThreadAttributes=None, bInheritHandles=False,
                   dwCreationFlags=0, lpEnvironment=None, lpCurrentDirectory=None, lpStartupInfo=None, lpProcessInformation=None):
    if lpStartupInfo is None:
        StartupInfo = STARTUPINFOA()
        StartupInfo.cb = ctypes.sizeof(StartupInfo)
        StartupInfo.dwFlags = STARTF_USESHOWWINDOW
        StartupInfo.wShowWindow = SW_HIDE
        lpStartupInfo = ctypes.byref(StartupInfo)
    if lpProcessInformation is None:
        lpProcessInformation = ctypes.byref(PROCESS_INFORMATION())
    return CreateProcessA.ctypes_function(lpApplicationName, lpCommandLine, lpProcessAttributes, lpThreadAttributes, bInheritHandles, dwCreationFlags, lpEnvironment, lpCurrentDirectory, lpStartupInfo, lpProcessInformation)


@Kernel32Proxy("CreateProcessW")
def CreateProcessW(lpApplicationName, lpCommandLine=None, lpProcessAttributes=None, lpThreadAttributes=None, bInheritHandles=False,
                   dwCreationFlags=0, lpEnvironment=None, lpCurrentDirectory=None, lpStartupInfo=None, lpProcessInformation=None):
    if lpStartupInfo is None:
        StartupInfo = STARTUPINFOW()
        StartupInfo.cb = ctypes.sizeof(StartupInfo)
        StartupInfo.dwFlags = STARTF_USESHOWWINDOW
        StartupInfo.wShowWindow = SW_HIDE
        lpStartupInfo = ctypes.byref(StartupInfo)
    if lpProcessInformation is None:
        lpProcessInformation = ctypes.byref(PROCESS_INFORMATION())
    return CreateProcessW.ctypes_function(lpApplicationName, lpCommandLine, lpProcessAttributes, lpThreadAttributes, bInheritHandles, dwCreationFlags, lpEnvironment, lpCurrentDirectory, lpStartupInfo, lpProcessInformation)



@Kernel32Proxy("GetThreadContext")
def GetThreadContext(hThread, lpContext=None):
    if lpContext is None:
        Context = CONTEXT()
        context.ContextFlags = CONTEXT_FULL | CONTEXT_DEBUG_REGISTERS
        lpContext = ctypes.byref(Context)
    return GetThreadContext.ctypes_function(hThread, lpContext)


@Kernel32Proxy("SetThreadContext")
def SetThreadContext(hThread, lpContext):
    return SetThreadContext.ctypes_function(hThread, lpContext)

@Kernel32Proxy("Wow64SetThreadContext")
def Wow64SetThreadContext(hThread, lpContext):
    return Wow64SetThreadContext.ctypes_function(hThread, lpContext)


@Kernel32Proxy("OpenThread")
def OpenThread(dwDesiredAccess=THREAD_ALL_ACCESS, bInheritHandle=0, dwThreadId=NeededParameter):
    return OpenThread.ctypes_function(dwDesiredAccess, bInheritHandle, dwThreadId)


@Kernel32Proxy("OpenProcess")
def OpenProcess(dwDesiredAccess=PROCESS_ALL_ACCESS, bInheritHandle=0, dwProcessId=NeededParameter):
    return OpenProcess.ctypes_function(dwDesiredAccess, bInheritHandle, dwProcessId)


@Kernel32Proxy("ReadProcessMemory")
def ReadProcessMemory(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesRead=None):
    return ReadProcessMemory.ctypes_function(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesRead)


@Kernel32Proxy("WriteProcessMemory")
def WriteProcessMemory(hProcess, lpBaseAddress, lpBuffer, nSize=None, lpNumberOfBytesWritten=None):
    """Computer nSize with len(lpBuffer) if not given"""
    if nSize is None:
        nSize = len(lpBuffer)
    return WriteProcessMemory.ctypes_function(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesWritten)


@Kernel32Proxy("GetProcessTimes")
def GetProcessTimes(hProcess, lpCreationTime, lpExitTime, lpKernelTime, lpUserTime):
    return GetProcessTimes.ctypes_function(hProcess, lpCreationTime, lpExitTime, lpKernelTime, lpUserTime)

@Kernel32Proxy('SetThreadAffinityMask')
def SetThreadAffinityMask(hThread=None, dwThreadAffinityMask=NeededParameter):
    """If hThread is not given, it will be the current thread"""
    if hThread is None:
        hThread = GetCurrentThread()
    return SetThreadAffinityMask.ctypes_function(hThread, dwThreadAffinityMask)


@Kernel32Proxy("CreateToolhelp32Snapshot")
def CreateToolhelp32Snapshot(dwFlags, th32ProcessID=0):
    return CreateToolhelp32Snapshot.ctypes_function(dwFlags, th32ProcessID)


@Kernel32Proxy("Thread32First")
def Thread32First(hSnapshot, lpte):
    """Set byref(lpte) if needed"""
    if type(lpte) == THREADENTRY32:
        lpte = ctypes.byref(lpte)
    return Thread32First.ctypes_function(hSnapshot, lpte)


@Kernel32Proxy("Thread32Next", no_error_check)
def Thread32Next(hSnapshot, lpte):
    """Set byref(lpte) if needed"""
    if type(lpte) == THREADENTRY32:
        lpte = ctypes.byref(lpte)
    return Thread32Next.ctypes_function(hSnapshot, lpte)


@Kernel32Proxy("Process32First")
def Process32First(hSnapshot, lpte):
    return Process32First.ctypes_function(hSnapshot, lpte)

@Kernel32Proxy("Process32Next", no_error_check)
def Process32Next(hSnapshot, lpte):
    return Process32Next.ctypes_function(hSnapshot, lpte)

@Kernel32Proxy("GetPriorityClass")
def GetPriorityClass(hProcess):
    return GetPriorityClass.ctypes_function(hProcess)

@Kernel32Proxy("SetPriorityClass")
def SetPriorityClass(hProcess, dwPriorityClass):
    return SetPriorityClass.ctypes_function(hProcess, dwPriorityClass)

@Kernel32Proxy("OpenEventA")
def OpenEventA(dwDesiredAccess, bInheritHandle, lpName):
    return OpenEventA.ctypes_function(dwDesiredAccess, bInheritHandle, lpName)

@Kernel32Proxy("OpenEventW")
def OpenEventW(dwDesiredAccess, bInheritHandle, lpName):
    return OpenEventA.ctypes_function(dwDesiredAccess, bInheritHandle, lpName)

# File stuff
@Kernel32Proxy("ReadFile")
def ReadFile(hFile, lpBuffer, nNumberOfBytesToRead=None, lpNumberOfBytesRead=None, lpOverlapped=None):
    if nNumberOfBytesToRead is None:
        nNumberOfBytesToRead = len(lpBuffer)
    if lpOverlapped is None and lpNumberOfBytesRead is None:
        lpNumberOfBytesRead = ctypes.byref(DWORD())
    return ReadFile.ctypes_function(hFile, lpBuffer, nNumberOfBytesToRead, lpNumberOfBytesRead, lpOverlapped)


@Kernel32Proxy("WriteFile")
def WriteFile(hFile, lpBuffer, nNumberOfBytesToWrite=None, lpNumberOfBytesWritten=None, lpOverlapped=None):
    if nNumberOfBytesToWrite is None:
        nNumberOfBytesToWrite = len(lpBuffer)
    if lpOverlapped is None and lpNumberOfBytesWritten is None:
        lpNumberOfBytesWritten = ctypes.byref(DWORD())
    return WriteFile.ctypes_function(hFile, lpBuffer, nNumberOfBytesToWrite, lpNumberOfBytesWritten, lpOverlapped)


# Exception stuff
@Kernel32Proxy("AddVectoredContinueHandler")
def AddVectoredContinueHandler(FirstHandler=1, VectoredHandler=NeededParameter):
    return AddVectoredContinueHandler.ctypes_function(FirstHandler, VectoredHandler)


@Kernel32Proxy("AddVectoredExceptionHandler")
def AddVectoredExceptionHandler(FirstHandler=1, VectoredHandler=NeededParameter):
    return AddVectoredExceptionHandler.ctypes_function(FirstHandler, VectoredHandler)


@Kernel32Proxy("RemoveVectoredExceptionHandler")
def RemoveVectoredExceptionHandler(Handler):
    return RemoveVectoredExceptionHandler.ctypes_function(Handler)


@Kernel32Proxy("WaitForSingleObject", should_return_zero_check)
def WaitForSingleObject(hHandle, dwMilliseconds=INFINITE):
    return WaitForSingleObject.ctypes_function(hHandle, dwMilliseconds)


@Kernel32Proxy("DeviceIoControl")
def DeviceIoControl(hDevice, dwIoControlCode, lpInBuffer, nInBufferSize=None, lpOutBuffer=NeededParameter, nOutBufferSize=None, lpBytesReturned=None, lpOverlapped=None):
    if nInBufferSize is None:
        nInBufferSize = len(lpInBuffer)
    if nOutBufferSize is None:
        nOutBufferSize = len(lpOutBuffer)
    if lpBytesReturned is None:
        # Some windows check 0 / others does not
        lpBytesReturned = ctypes.byref(DWORD())
    return DeviceIoControl.ctypes_function(hDevice, dwIoControlCode, lpInBuffer, nInBufferSize, lpOutBuffer, nOutBufferSize, lpBytesReturned, lpOverlapped)



@Kernel32Proxy("CreateFileMappingA")
def CreateFileMappingA(hFile, lpFileMappingAttributes=None, flProtect=PAGE_READWRITE, dwMaximumSizeHigh=0, dwMaximumSizeLow=NeededParameter, lpName=NeededParameter):
    return CreateFileMappingA.ctypes_function(hFile, lpFileMappingAttributes, flProtect, dwMaximumSizeHigh, dwMaximumSizeLow, lpName)


@Kernel32Proxy("CreateFileMappingW")
def CreateFileMappingW(hFile, lpFileMappingAttributes=None, flProtect=PAGE_READWRITE, dwMaximumSizeHigh=0, dwMaximumSizeLow=0, lpName=NeededParameter):
    return CreateFileMappingW.ctypes_function(hFile, lpFileMappingAttributes, flProtect, dwMaximumSizeHigh, dwMaximumSizeLow, lpName)


@Kernel32Proxy("MapViewOfFile")
def MapViewOfFile(hFileMappingObject, dwDesiredAccess=FILE_MAP_ALL_ACCESS, dwFileOffsetHigh=0, dwFileOffsetLow=0, dwNumberOfBytesToMap=NeededParameter):
    return MapViewOfFile.ctypes_function(hFileMappingObject, dwDesiredAccess, dwFileOffsetHigh, dwFileOffsetLow, dwNumberOfBytesToMap)


@Kernel32Proxy("DuplicateHandle")
def DuplicateHandle(hSourceProcessHandle, hSourceHandle, hTargetProcessHandle, lpTargetHandle, dwDesiredAccess=0, bInheritHandle=False, dwOptions=0):
    return DuplicateHandle.ctypes_function(hSourceProcessHandle, hSourceHandle, hTargetProcessHandle, lpTargetHandle, dwDesiredAccess, bInheritHandle, dwOptions)

@Kernel32Proxy("GetLongPathNameA")
def GetLongPathNameA(lpszShortPath, lpszLongPath, cchBuffer=None):
    if cchBuffer is None:
        cchBuffer = len(lpszLongPath)
    return GetLongPathNameA.ctypes_function(lpszShortPath, lpszLongPath, cchBuffer)

@Kernel32Proxy("GetLongPathNameW")
def GetLongPathNameW(lpszShortPath, lpszLongPath, cchBuffer=None):
    if cchBuffer is None:
        cchBuffer = len(lpszLongPath)
    return GetLongPathNameW.ctypes_function(lpszShortPath, lpszLongPath, cchBuffer)

@Kernel32Proxy("GetShortPathNameA")
def GetShortPathNameA(lpszLongPath, lpszShortPath, cchBuffer=None):
    if cchBuffer is None:
        cchBuffer = len(lpszShortPath)
    return GetShortPathNameA.ctypes_function(lpszLongPath, lpszShortPath, cchBuffer)

@Kernel32Proxy("GetShortPathNameW")
def GetShortPathNameW(lpszLongPath, lpszShortPath, cchBuffer=None):
    if cchBuffer is None:
        cchBuffer = len(lpszShortPath)
    return GetShortPathNameW.ctypes_function(lpszLongPath, lpszShortPath, cchBuffer)

# TODO: might be in another DLL depending of version
# Should handle this..

def GetMappedFileNameWWrapper(hProcess, lpv, lpFilename, nSize=None):
    if nSize is None:
        nSize = ctypes.sizeof(lpFilename)
    return GetMappedFileNameWWrapper.ctypes_function(hProcess, lpv, lpFilename, nSize)
GetMappedFileNameW = Kernel32Proxy("GetMappedFileNameW")(GetMappedFileNameWWrapper)


def GetMappedFileNameAWrapper(hProcess, lpv, lpFilename, nSize=None):
    if nSize is None:
        nSize = ctypes.sizeof(lpFilename)
    return GetMappedFileNameAWrapper.ctypes_function(hProcess, lpv, lpFilename, nSize)
GetMappedFileNameA = Kernel32Proxy("GetMappedFileNameA")(GetMappedFileNameAWrapper)

def QueryWorkingSetWrapper(hProcess, pv, cb):
    return QueryWorkingSet.ctypes_function(hProcess, pv, cb)
QueryWorkingSet = Kernel32Proxy("QueryWorkingSet")(QueryWorkingSetWrapper)

def QueryWorkingSetExWrapper(hProcess, pv, cb):
    return QueryWorkingSetEx.ctypes_function(hProcess, pv, cb)
QueryWorkingSetEx = Kernel32Proxy("QueryWorkingSetEx")(QueryWorkingSetExWrapper)



if not is_implemented(GetMappedFileNameA):
    GetMappedFileNameW = PsapiProxy("GetMappedFileNameW")(GetMappedFileNameWWrapper)
    GetMappedFileNameA = PsapiProxy("GetMappedFileNameA")(GetMappedFileNameAWrapper)
    QueryWorkingSet = PsapiProxy("QueryWorkingSet")(QueryWorkingSetWrapper)
    QueryWorkingSetEx = PsapiProxy("QueryWorkingSetEx")(QueryWorkingSetExWrapper)

def GetModuleBaseNameAWrapper(hProcess, hModule, lpBaseName, nSize=None):
    if nSize is None:
        nSize = len(lpBaseName)
    return GetModuleBaseNameAWrapper.ctypes_function(hProcess, hModule, lpBaseName, nSize)
GetModuleBaseNameA = Kernel32Proxy("GetMappedFileNameA")(GetModuleBaseNameAWrapper)


def GetModuleBaseNameWWrapper(hProcess, hModule, lpBaseName, nSize=None):
    if nSize is None:
        nSize = len(lpBaseName)
    return GetModuleBaseNameWWrapper.ctypes_function(hProcess, hModule, lpBaseName, nSize)
GetModuleBaseNameA = Kernel32Proxy("GetModuleBaseNameW")(GetModuleBaseNameWWrapper)

if not is_implemented(GetModuleBaseNameA):
    GetModuleBaseNameA = PsapiProxy("GetModuleBaseNameA")(GetModuleBaseNameAWrapper)
    GetModuleBaseNameW = PsapiProxy("GetModuleBaseNameW")(GetModuleBaseNameWWrapper)


def GetProcessImageFileNameAWrapper(hProcess, lpImageFileName, nSize=None):
    if nSize is None:
        nSize = len(lpImageFileName)
    return GetProcessImageFileNameAWrapper.ctypes_function(hProcess, lpImageFileName, nSize)
GetProcessImageFileNameA = Kernel32Proxy("GetProcessImageFileNameA")(GetProcessImageFileNameAWrapper)

def GetProcessImageFileNameWWrapper(hProcess, lpImageFileName, nSize=None):
    if nSize is None:
        nSize = len(lpImageFileName)
    return GetProcessImageFileNameWWrapper.ctypes_function(hProcess, lpImageFileName, nSize)
GetProcessImageFileNameW = Kernel32Proxy("GetProcessImageFileNameW")(GetProcessImageFileNameWWrapper)

if not is_implemented(GetProcessImageFileNameA):
    GetProcessImageFileNameA = PsapiProxy("GetProcessImageFileNameA")(GetProcessImageFileNameAWrapper)
    GetProcessImageFileNameW = PsapiProxy("GetProcessImageFileNameW")(GetProcessImageFileNameWWrapper)

# Debug API

DebugBreak = TransparentKernel32Proxy("DebugBreak")
ContinueDebugEvent = TransparentKernel32Proxy("ContinueDebugEvent")
DebugActiveProcess = TransparentKernel32Proxy("DebugActiveProcess")
DebugActiveProcessStop = TransparentKernel32Proxy("DebugActiveProcessStop")
DebugSetProcessKillOnExit = TransparentKernel32Proxy("DebugSetProcessKillOnExit")
DebugBreakProcess = TransparentKernel32Proxy("DebugBreakProcess")

@Kernel32Proxy("WaitForDebugEvent")
def WaitForDebugEvent(lpDebugEvent, dwMilliseconds=INFINITE):
    return WaitForDebugEvent.ctypes_function(lpDebugEvent, dwMilliseconds)


# Volumes stuff

GetLogicalDriveStringsA = TransparentKernel32Proxy("GetLogicalDriveStringsA")
GetLogicalDriveStringsW = TransparentKernel32Proxy("GetLogicalDriveStringsW")
GetDriveTypeA = TransparentKernel32Proxy("GetDriveTypeA")
GetDriveTypeW = TransparentKernel32Proxy("GetDriveTypeW")
QueryDosDeviceA = TransparentKernel32Proxy("QueryDosDeviceA")
QueryDosDeviceW = TransparentKernel32Proxy("QueryDosDeviceW")
GetVolumeNameForVolumeMountPointA = TransparentKernel32Proxy("GetVolumeNameForVolumeMountPointA")
GetVolumeNameForVolumeMountPointW = TransparentKernel32Proxy("GetVolumeNameForVolumeMountPointW")



@Kernel32Proxy("GetVolumeInformationA")
def GetVolumeInformationA(lpRootPathName, lpVolumeNameBuffer, nVolumeNameSize, lpVolumeSerialNumber, lpMaximumComponentLength, lpFileSystemFlags, lpFileSystemNameBuffer, nFileSystemNameSize):
    if nVolumeNameSize == 0 and lpVolumeNameBuffer is not None:
        nVolumeNameSize = len(lpVolumeNameBuffer)
    if nFileSystemNameSize == 0 and lpFileSystemNameBuffer is not None:
        nFileSystemNameSize = len(lpFileSystemNameBuffer)
    return GetVolumeInformationA.ctypes_function(lpRootPathName, lpVolumeNameBuffer, nVolumeNameSize, lpVolumeSerialNumber, lpMaximumComponentLength, lpFileSystemFlags, lpFileSystemNameBuffer, nFileSystemNameSize)


@Kernel32Proxy("GetVolumeInformationW")
def GetVolumeInformationW(lpRootPathName, lpVolumeNameBuffer=None, nVolumeNameSize=0, lpVolumeSerialNumber=None, lpMaximumComponentLength=None, lpFileSystemFlags=None, lpFileSystemNameBuffer=None, nFileSystemNameSize=0):
    if nVolumeNameSize == 0 and lpVolumeNameBuffer is not None:
        nVolumeNameSize = len(lpVolumeNameBuffer)
    if nFileSystemNameSize == 0 and lpFileSystemNameBuffer is not None:
        nFileSystemNameSize = len(lpFileSystemNameBuffer)
    return GetVolumeInformationW.ctypes_function(lpRootPathName, lpVolumeNameBuffer, nVolumeNameSize, lpVolumeSerialNumber, lpMaximumComponentLength, lpFileSystemFlags, lpFileSystemNameBuffer, nFileSystemNameSize)


@Kernel32Proxy("FindFirstVolumeA")
def FindFirstVolumeA(lpszVolumeName, cchBufferLength):
    if cchBufferLength is None:
        cchBufferLength = len(lpszVolumeName)
    return FindFirstVolumeA.ctypes_function(lpszVolumeName, cchBufferLength)


@Kernel32Proxy("FindFirstVolumeW")
def FindFirstVolumeW(lpszVolumeName, cchBufferLength):
    if cchBufferLength is None:
        cchBufferLength = len(lpszVolumeName)
    return FindFirstVolumeW.ctypes_function(lpszVolumeName, cchBufferLength)



@Kernel32Proxy("FindNextVolumeA")
def FindNextVolumeA(hFindVolume, lpszVolumeName, cchBufferLength):
    if cchBufferLength is None:
        cchBufferLength = len(lpszVolumeName)
    return FindNextVolumeA.ctypes_function(hFindVolume, lpszVolumeName, cchBufferLength)


@Kernel32Proxy("FindNextVolumeW")
def FindNextVolumeW(hFindVolume, lpszVolumeName, cchBufferLength):
    if cchBufferLength is None:
        cchBufferLength = len(lpszVolumeName)
    return FindNextVolumeW.ctypes_function(hFindVolume, lpszVolumeName, cchBufferLength)


@Kernel32Proxy("SetConsoleCtrlHandler")
def SetConsoleCtrlHandler(HandlerRoutine, Add):
    return SetConsoleCtrlHandler.ctypes_function(HandlerRoutine, Add)

@Kernel32Proxy("GetProcessDEPPolicy")
def GetProcessDEPPolicy(hProcess, lpFlags, lpPermanent):
    return GetProcessDEPPolicy.ctypes_function(hProcess, lpFlags, lpPermanent)


# ProcThreadAttributeList
def initializeprocthreadattributelist_error_check(func_name, result, func, args):
    if result:
        return args
    error = GetLastError()
    if error == ERROR_INSUFFICIENT_BUFFER and args[0] is None:
        return args
    raise Kernel32Error(func_name)

@Kernel32Proxy("InitializeProcThreadAttributeList", initializeprocthreadattributelist_error_check)
def InitializeProcThreadAttributeList(lpAttributeList=None, dwAttributeCount=NeededParameter, dwFlags=0, lpSize=NeededParameter):
    return InitializeProcThreadAttributeList.ctypes_function(lpAttributeList, dwAttributeCount, dwFlags, lpSize)


@Kernel32Proxy("UpdateProcThreadAttribute")
def UpdateProcThreadAttribute(lpAttributeList, dwFlags=0, Attribute=NeededParameter, lpValue=NeededParameter, cbSize=NeededParameter, lpPreviousValue=None, lpReturnSize=None):
    return UpdateProcThreadAttribute.ctypes_function(lpAttributeList, dwFlags, Attribute, lpValue, cbSize, lpPreviousValue, lpReturnSize)


@Kernel32Proxy("DeleteProcThreadAttributeList")
def DeleteProcThreadAttributeList(lpAttributeList):
    return DeleteProcThreadAttributeList.ctypes_function(lpAttributeList)


@Kernel32Proxy("GetWindowsDirectoryA")
def GetWindowsDirectoryA(lpBuffer, uSize=None):
    if uSize is None:
        uSize = DWORD(len(lpBuffer))
    return GetWindowsDirectoryA.ctypes_function(lpBuffer, uSize)


@Kernel32Proxy("GetWindowsDirectoryW")
def GetWindowsDirectoryW(lpBuffer, uSize=None):
    if uSize is None:
        uSize = DWORD(len(lpBuffer))
    return GetWindowsDirectoryW.ctypes_function(lpBuffer, uSize)


# ### NTDLL #### #

@NtdllProxy('NtReadVirtualMemory', error_ntstatus)
def NtReadVirtualMemory(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesRead):
    return NtReadVirtualMemory.ctypes_function(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesRead)

@NtdllProxy('NtWow64ReadVirtualMemory64', error_ntstatus)
def NtWow64ReadVirtualMemory64(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesRead=None):
    return NtWow64ReadVirtualMemory64.ctypes_function(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesRead)

@NtdllProxy('NtWow64WriteVirtualMemory64', error_ntstatus)
def NtWow64WriteVirtualMemory64(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesWritten=None):
    return NtWow64WriteVirtualMemory64.ctypes_function(hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesWritten)

def ntquerysysteminformation_error_check(func_name, result, func, args):
    if result == 0:
        return args
    # Ignore STATUS_INFO_LENGTH_MISMATCH if SystemInformation is None
    if result == STATUS_INFO_LENGTH_MISMATCH and args[1] is None:
        return args
    raise Kernel32Error("{0} failed with NTStatus {1}".format(func_name, hex(result)))

@NtdllProxy("NtCreateFile", error_ntstatus)
def NtCreateFile(FileHandle, DesiredAccess, ObjectAttributes, IoStatusBlock, AllocationSize, FileAttributes, ShareAccess, CreateDisposition, CreateOptions, EaBuffer, EaLength):
    return NtCreateFile.ctypes_function(FileHandle, DesiredAccess, ObjectAttributes, IoStatusBlock, AllocationSize, FileAttributes, ShareAccess, CreateDisposition, CreateOptions, EaBuffer, EaLength)

@NtdllProxy("NtGetContextThread", error_ntstatus)
def NtGetContextThread(hThread, lpContext):
    return NtGetContextThread.ctypes_function(hThread, lpContext)

@NtdllProxy("LdrLoadDll", error_ntstatus)
def LdrLoadDll(PathToFile, Flags, ModuleFileName, ModuleHandle):
    return LdrLoadDll.ctypes_function(PathToFile, Flags, ModuleFileName, ModuleHandle)


@NtdllProxy('NtQuerySystemInformation', ntquerysysteminformation_error_check)
def NtQuerySystemInformation(SystemInformationClass, SystemInformation=None, SystemInformationLength=0, ReturnLength=NeededParameter):
    if SystemInformation is not None and SystemInformationLength == 0:
        SystemInformationLength = ctypes.sizeof(SystemInformation)
    return NtQuerySystemInformation.ctypes_function(SystemInformationClass, SystemInformation, SystemInformationLength, ReturnLength)


@NtdllProxy('NtQueryInformationProcess', error_ntstatus)
def NtQueryInformationProcess(ProcessHandle, ProcessInformationClass, ProcessInformation, ProcessInformationLength=0, ReturnLength=None):
    if ProcessInformation is not None and ProcessInformationLength == 0:
        ProcessInformationLength = ctypes.sizeof(ProcessInformation)
    if type(ProcessInformation) == PROCESS_BASIC_INFORMATION:
        ProcessInformation = byref(ProcessInformation)
    if ReturnLength is None:
        ReturnLength = byref(ULONG())
    return NtQueryInformationProcess.ctypes_function(ProcessHandle, ProcessInformationClass, ProcessInformation, ProcessInformationLength, ReturnLength)


@NtdllProxy('NtSetInformationProcess', error_ntstatus)
def NtSetInformationProcess(ProcessHandle, ProcessInformationClass, ProcessInformation, ProcessInformationLength=0):
    if not ProcessInformationLength:
        ProcessInformationLength = ctypes.sizeof(ProcessInformation)
    return NtSetInformationProcess.ctypes_function(ProcessHandle, ProcessInformationClass, ProcessInformation, ProcessInformationLength)

@NtdllProxy('NtQueryInformationThread', error_ntstatus)
def NtQueryInformationThread(ThreadHandle, ThreadInformationClass, ThreadInformation, ThreadInformationLength=0, ReturnLength=None):
    if ReturnLength is None:
        ReturnLength = byref(ULONG())
    if ThreadInformation is not None and ThreadInformationLength == 0:
        ThreadInformationLength = ctypes.sizeof(ThreadInformation)
    return NtQueryInformationThread.ctypes_function(ThreadHandle, ThreadInformationClass, ThreadInformation, ThreadInformationLength, ReturnLength)


@NtdllProxy('NtProtectVirtualMemory', error_ntstatus)
def NtProtectVirtualMemory(ProcessHandle, BaseAddress, NumberOfBytesToProtect, NewAccessProtection, OldAccessProtection=None):
    if OldAccessProtection is None:
        OldAccessProtection = DWORD()
    return NtProtectVirtualMemory.ctypes_function(ProcessHandle, BaseAddress, NumberOfBytesToProtect, NewAccessProtection, OldAccessProtection)

@NtdllProxy('NtQueryVirtualMemory', error_ntstatus)
def NtQueryVirtualMemory(ProcessHandle, BaseAddress, MemoryInformationClass, MemoryInformation=NeededParameter, MemoryInformationLength=0, ReturnLength=None):
    if ReturnLength is None:
        ReturnLength = byref(ULONG())
    if MemoryInformation is not None and MemoryInformationLength == 0:
        ProcessInformationLength = ctypes.sizeof(MemoryInformation)
    if type(MemoryInformation) == MEMORY_BASIC_INFORMATION64:
        MemoryInformation = byref(MemoryInformation)
    return NtQueryVirtualMemory.ctypes_function(ProcessHandle, BaseAddress, MemoryInformationClass, MemoryInformation=NeededParameter, MemoryInformationLength=0, ReturnLength=None)


@NtdllProxy('NtQueryObject', error_ntstatus)
def NtQueryObject(Handle, ObjectInformationClass, ObjectInformation=None, ObjectInformationLength=0, ReturnLength=NeededParameter):
    return NtQueryObject.ctypes_function(Handle, ObjectInformationClass, ObjectInformation, ObjectInformationLength, ReturnLength)

@NtdllProxy('NtCreateThreadEx', error_ntstatus)
def NtCreateThreadEx(ThreadHandle=None, DesiredAccess=0x1fffff, ObjectAttributes=0, ProcessHandle=NeededParameter, lpStartAddress=NeededParameter, lpParameter=NeededParameter, CreateSuspended=0, dwStackSize=0, Unknown1=0, Unknown2=0, Unknown=0):
    if ThreadHandle is None:
        ThreadHandle = byref(HANDLE())
    return NtCreateThreadEx.ctypes_function(ThreadHandle, DesiredAccess, ObjectAttributes, ProcessHandle, lpStartAddress, lpParameter, CreateSuspended, dwStackSize, Unknown1, Unknown2, Unknown3)


@NtdllProxy("NtSetContextThread", error_ntstatus)
def NtSetContextThread(hThread, lpContext):
    return NtSetContextThread.ctypes_function(hThread, lpContext)

@NtdllProxy("NtOpenEvent", error_ntstatus)
def NtOpenEvent(EventHandle, DesiredAccess, ObjectAttributes):
    return NtOpenEvent.ctypes_function(EventHandle, DesiredAccess, ObjectAttributes)


@NtdllProxy("NtAlpcCreatePort", error_ntstatus)
def NtAlpcCreatePort(PortHandle, ObjectAttributes, PortAttributes):
    return NtAlpcCreatePort.ctypes_function(PortHandle, ObjectAttributes, PortAttributes)


@NtdllProxy("NtAlpcConnectPort", error_ntstatus)
def NtAlpcConnectPort(PortHandle, PortName, ObjectAttributes, PortAttributes, Flags, RequiredServerSid, ConnectionMessage, BufferLength, OutMessageAttributes, InMessageAttributes, Timeout):
    return NtAlpcConnectPort.ctypes_function(PortHandle, PortName, ObjectAttributes, PortAttributes, Flags, RequiredServerSid, ConnectionMessage, BufferLength, OutMessageAttributes, InMessageAttributes, Timeout)


@NtdllProxy("NtAlpcConnectPortEx", error_ntstatus)
def NtAlpcConnectPortEx(PortHandle, ConnectionPortObjectAttributes, ClientPortObjectAttributes, PortAttributes, Flags, ServerSecurityRequirements, ConnectionMessage, BufferLength, OutMessageAttributes, InMessageAttributes, Timeout):
    return NtAlpcConnectPortEx.ctypes_function(PortHandle, ConnectionPortObjectAttributes, ClientPortObjectAttributes, PortAttributes, Flags, ServerSecurityRequirements, ConnectionMessage, BufferLength, OutMessageAttributes, InMessageAttributes, Timeout)


@NtdllProxy("NtAlpcAcceptConnectPort", error_ntstatus)
def NtAlpcAcceptConnectPort(PortHandle, ConnectionPortHandle, Flags, ObjectAttributes, PortAttributes, PortContext, ConnectionRequest, ConnectionMessageAttributes, AcceptConnection):
    return NtAlpcAcceptConnectPort.ctypes_function(PortHandle, ConnectionPortHandle, Flags, ObjectAttributes, PortAttributes, PortContext, ConnectionRequest, ConnectionMessageAttributes, AcceptConnection)


@NtdllProxy("NtAlpcQueryInformation", error_ntstatus)
def NtAlpcQueryInformation(PortHandle, PortInformationClass, PortInformation, Length, ReturnLength):
    return NtAlpcQueryInformation.ctypes_function(PortHandle, PortInformationClass, PortInformation, Length, ReturnLength)

@NtdllProxy("NtAlpcDisconnectPort", error_ntstatus)
def NtAlpcDisconnectPort(PortHandle, Flags):
    return NtAlpcDisconnectPort.ctypes_function(PortHandle, Flags)

@NtdllProxy("NtAlpcSendWaitReceivePort", error_ntstatus)
def NtAlpcSendWaitReceivePort(PortHandle, Flags, SendMessage, SendMessageAttributes, ReceiveMessage, BufferLength, ReceiveMessageAttributes, Timeout):
    return NtAlpcSendWaitReceivePort.ctypes_function(PortHandle, Flags, SendMessage, SendMessageAttributes, ReceiveMessage, BufferLength, ReceiveMessageAttributes, Timeout)


@NtdllProxy("AlpcInitializeMessageAttribute", error_ntstatus)
def AlpcInitializeMessageAttribute(AttributeFlags, Buffer, BufferSize, RequiredBufferSize):
    return AlpcInitializeMessageAttribute.ctypes_function(AttributeFlags, Buffer, BufferSize, RequiredBufferSize)


@NtdllProxy("AlpcGetMessageAttribute", no_error_check)
def AlpcGetMessageAttribute(Buffer, AttributeFlag):
    return AlpcGetMessageAttribute.ctypes_function(Buffer, AttributeFlag)


@NtdllProxy("NtAlpcCreatePortSection", error_ntstatus)
def NtAlpcCreatePortSection(PortHandle, Flags, SectionHandle, SectionSize, AlpcSectionHandle, ActualSectionSize):
    return NtAlpcCreatePortSection.ctypes_function(PortHandle, Flags, SectionHandle, SectionSize, AlpcSectionHandle, ActualSectionSize)


@NtdllProxy("NtAlpcDeletePortSection", error_ntstatus)
def NtAlpcDeletePortSection(PortHandle, Flags, SectionHandle):
    return NtAlpcDeletePortSection.ctypes_function(PortHandle, Flags, SectionHandle)


@NtdllProxy("NtAlpcCreateSectionView", error_ntstatus)
def NtAlpcCreateSectionView(PortHandle, Flags, ViewAttributes):
    return NtAlpcCreateSectionView.ctypes_function(PortHandle, Flags, ViewAttributes)


@NtdllProxy("NtAlpcDeleteSectionView", error_ntstatus)
def NtAlpcDeleteSectionView(PortHandle, Flags, ViewBase):
    return NtAlpcDeleteSectionView.ctypes_function(PortHandle, Flags, ViewBase)


@NtdllProxy("NtAlpcQueryInformationMessage", error_ntstatus)
def NtAlpcQueryInformationMessage(PortHandle, PortMessage, MessageInformationClass, MessageInformation, Length, ReturnLength):
    return NtAlpcQueryInformationMessage.ctypes_function(PortHandle, PortMessage, MessageInformationClass, MessageInformation, Length, ReturnLength)


@NtdllProxy("NtOpenDirectoryObject", error_ntstatus)
def NtOpenDirectoryObject(DirectoryHandle, DesiredAccess, ObjectAttributes):
    return NtOpenDirectoryObject.ctypes_function(DirectoryHandle, DesiredAccess, ObjectAttributes)


@NtdllProxy("NtQueryDirectoryObject", error_ntstatus)
def NtQueryDirectoryObject(DirectoryHandle, Buffer, Length, ReturnSingleEntry, RestartScan, Context, ReturnLength):
    return NtQueryDirectoryObject.ctypes_function(DirectoryHandle, Buffer, Length, ReturnSingleEntry, RestartScan, Context, ReturnLength)


@NtdllProxy("NtQuerySymbolicLinkObject", error_ntstatus)
def NtQuerySymbolicLinkObject(LinkHandle, LinkTarget, ReturnedLength):
    return NtQuerySymbolicLinkObject.ctypes_function(LinkHandle, LinkTarget, ReturnedLength)


@NtdllProxy("NtOpenSymbolicLinkObject", error_ntstatus)
def NtOpenSymbolicLinkObject(LinkHandle, DesiredAccess, ObjectAttributes):
    return NtOpenSymbolicLinkObject.ctypes_function(LinkHandle, DesiredAccess, ObjectAttributes)


@NtdllProxy("NtQueryInformationFile", error_ntstatus)
def NtQueryInformationFile(FileHandle, IoStatusBlock, FileInformation, Length=None, FileInformationClass=NeededParameter):
    if Length is None:
        Length = ctypes.sizeof(FileInformation)
    return NtQueryInformationFile.ctypes_function(FileHandle, IoStatusBlock, FileInformation, Length, FileInformationClass)


@NtdllProxy("NtQueryDirectoryFile", error_ntstatus)
def NtQueryDirectoryFile(FileHandle, Event=None, ApcRoutine=None, ApcContext=None, IoStatusBlock=NeededParameter, FileInformation=NeededParameter, Length=None, FileInformationClass=NeededParameter, ReturnSingleEntry=NeededParameter, FileName=None, RestartScan=NeededParameter):
    if Length is None:
        Length = ctypes.sizeof(FileInformation)
    return NtQueryDirectoryFile.ctypes_function(FileHandle, Event, ApcRoutine, ApcContext, IoStatusBlock, FileInformation, Length, FileInformationClass, ReturnSingleEntry, FileName, RestartScan)

@NtdllProxy("NtQueryVolumeInformationFile", error_ntstatus)
def NtQueryVolumeInformationFile(FileHandle, IoStatusBlock, FsInformation, Length=None, FsInformationClass=NeededParameter):
    if Length is None:
        Length = ctypes.sizeof(FsInformation)
    return NtQueryVolumeInformationFile.ctypes_function(FileHandle, IoStatusBlock, FsInformation, Length, FsInformationClass)


@NtdllProxy("RtlDecompressBuffer", error_ntstatus)
def RtlDecompressBuffer(CompressionFormat, UncompressedBuffer, UncompressedBufferSize, CompressedBuffer, CompressedBufferSize=None, FinalUncompressedSize=NeededParameter):
    if CompressedBufferSize is None:
        CompressedBufferSize = len(CompressedBuffer)
    return RtlDecompressBuffer.ctypes_function(CompressionFormat, UncompressedBuffer, UncompressedBufferSize, CompressedBuffer, CompressedBufferSize, FinalUncompressedSize)

@NtdllProxy("RtlDecompressBufferEx", error_ntstatus)
def RtlDecompressBufferEx(CompressionFormat, UncompressedBuffer, UncompressedBufferSize, CompressedBuffer, CompressedBufferSize=None, FinalUncompressedSize=NeededParameter, WorkSpace=NeededParameter):
    if CompressedBufferSize is None:
        CompressedBufferSize = len(CompressedBuffer)
    # TODO: automatic 'WorkSpace' size calc + allocation ?
    return RtlDecompressBufferEx.ctypes_function(CompressionFormat, UncompressedBuffer, UncompressedBufferSize, CompressedBuffer, CompressedBufferSize, FinalUncompressedSize, WorkSpace)

@NtdllProxy("RtlGetCompressionWorkSpaceSize", error_ntstatus)
def RtlGetCompressionWorkSpaceSize(CompressionFormatAndEngine, CompressBufferWorkSpaceSize, CompressFragmentWorkSpaceSize):
    return RtlGetCompressionWorkSpaceSize.ctypes_function(CompressionFormatAndEngine, CompressBufferWorkSpaceSize, CompressFragmentWorkSpaceSize)


@NtdllProxy("RtlDosPathNameToNtPathName_U", zero_is_fail_error_check)
def RtlDosPathNameToNtPathName_U(DosName, NtName=None, PartName=None, RelativeName=None):
    return RtlDosPathNameToNtPathName_U.ctypes_function(DosName, NtName, PartName, RelativeName)


# Section stuff

@NtdllProxy("NtCreateSection", error_ntstatus)
def NtCreateSection(SectionHandle, DesiredAccess, ObjectAttributes, MaximumSize, SectionPageProtection, AllocationAttributes, FileHandle):
    return NtCreateSection.ctypes_function(SectionHandle, DesiredAccess, ObjectAttributes, MaximumSize, SectionPageProtection, AllocationAttributes, FileHandle)


@NtdllProxy("NtOpenSection", error_ntstatus)
def NtOpenSection(SectionHandle, DesiredAccess, ObjectAttributes):
    return NtOpenSection.ctypes_function(SectionHandle, DesiredAccess, ObjectAttributes)


@NtdllProxy("NtMapViewOfSection", error_ntstatus)
def NtMapViewOfSection(SectionHandle, ProcessHandle, BaseAddress, ZeroBits, CommitSize, SectionOffset, ViewSize, InheritDisposition, AllocationType, Win32Protect):
    return NtMapViewOfSection.ctypes_function(SectionHandle, ProcessHandle, BaseAddress, ZeroBits, CommitSize, SectionOffset, ViewSize, InheritDisposition, AllocationType, Win32Protect)


@NtdllProxy("NtUnmapViewOfSection", error_ntstatus)
def NtUnmapViewOfSection(ProcessHandle, BaseAddress):
    return NtUnmapViewOfSection.ctypes_function(ProcessHandle, BaseAddress)


@NtdllProxy("RtlGetUnloadEventTraceEx", no_error_check)
def RtlGetUnloadEventTraceEx(ElementSize, ElementCount, EventTrace):
    return RtlGetUnloadEventTraceEx.ctypes_function(ElementSize, ElementCount, EventTrace)


@NtdllProxy("TpCallbackSendAlpcMessageOnCompletion")
def TpCallbackSendAlpcMessageOnCompletion(TpHandle, PortHandle, Flags, SendMessage):
    return TpCallbackSendAlpcMessageOnCompletion.ctypes_function(TpHandle, PortHandle, Flags, SendMessage)

# Not exported

# @NtdllProxy("ApiSetResolveToHost")
# def ApiSetResolveToHost(Schema, FileNameIn, ParentName, Resolved, HostBinary):
    # return ApiSetResolveToHost.ctypes_function(Schema, FileNameIn, ParentName, Resolved, HostBinary)


# ##### ADVAPI32 ####### #

@Advapi32Proxy('OpenProcessToken')
def OpenProcessToken(ProcessHandle=None, DesiredAccess=NeededParameter, TokenHandle=NeededParameter):
    """If ProcessHandle is None: take the current process"""
    if ProcessHandle is None:
        ProcessHandle = GetCurrentProcess()
    return OpenProcessToken.ctypes_function(ProcessHandle, DesiredAccess, TokenHandle)


@Advapi32Proxy('OpenThreadToken')
def OpenThreadToken(ThreadHandle, DesiredAccess, OpenAsSelf, TokenHandle):
    return OpenThreadToken.ctypes_function(ThreadHandle, DesiredAccess, OpenAsSelf, TokenHandle)

@Advapi32Proxy('LookupPrivilegeValueA')
def LookupPrivilegeValueA(lpSystemName=None, lpName=NeededParameter, lpLuid=NeededParameter):
    return LookupPrivilegeValueA.ctypes_function(lpSystemName, lpName, lpLuid)


@Advapi32Proxy('LookupPrivilegeValueW')
def LookupPrivilegeValueW(lpSystemName=None, lpName=NeededParameter, lpLuid=NeededParameter):
    return LookupPrivilegeValueW.ctypes_function(lpSystemName, lpName, lpLuid)

@Advapi32Proxy('LookupPrivilegeNameA')
def LookupPrivilegeNameA(lpSystemName, lpLuid, lpName, cchName):
    return LookupPrivilegeNameA.ctypes_function(lpSystemName, lpLuid, lpName, cchName)

@Advapi32Proxy('LookupPrivilegeNameW')
def LookupPrivilegeNameW(lpSystemName, lpLuid, lpName, cchName):
    return LookupPrivilegeNameW.ctypes_function(lpSystemName, lpLuid, lpName, cchName)


@Advapi32Proxy('AdjustTokenPrivileges')
def AdjustTokenPrivileges(TokenHandle, DisableAllPrivileges=False, NewState=NeededParameter, BufferLength=None, PreviousState=None, ReturnLength=None):
    if BufferLength is None:
        BufferLength = ctypes.sizeof(NewState)
    return AdjustTokenPrivileges.ctypes_function(TokenHandle, DisableAllPrivileges, NewState, BufferLength, PreviousState, ReturnLength)


@Advapi32Proxy('LookupAccountSidA')
def LookupAccountSidA(lpSystemName, lpSid, lpName, cchName, lpReferencedDomainName, cchReferencedDomainName, peUse):
    return LookupAccountSidA.ctypes_function(lpSystemName, lpSid, lpName, cchName, lpReferencedDomainName, cchReferencedDomainName, peUse)


@Advapi32Proxy('LookupAccountSidW')
def LookupAccountSidW(lpSystemName, lpSid, lpName, cchName, lpReferencedDomainName, cchReferencedDomainName, peUse):
    return LookupAccountSidW.ctypes_function(lpSystemName, lpSid, lpName, cchName, lpReferencedDomainName, cchReferencedDomainName, peUse)

@Advapi32Proxy('CreateWellKnownSid')
def CreateWellKnownSid(WellKnownSidType, DomainSid=None, pSid=None, cbSid=NeededParameter):
    return CreateWellKnownSid.ctypes_function(WellKnownSidType, DomainSid, pSid, cbSid)

# Token stuff

GetSidSubAuthorityCount = TransparentAdvapi32Proxy("GetSidSubAuthorityCount")
GetSidSubAuthority = TransparentAdvapi32Proxy("GetSidSubAuthority")
GetLengthSid = TransparentAdvapi32Proxy("GetLengthSid")

@Advapi32Proxy('GetTokenInformation')
def GetTokenInformation(TokenHandle=NeededParameter, TokenInformationClass=NeededParameter, TokenInformation=None, TokenInformationLength=0, ReturnLength=None):
    if ReturnLength is None:
        ReturnLength = ctypes.byref(DWORD())
    return GetTokenInformation.ctypes_function(TokenHandle, TokenInformationClass, TokenInformation, TokenInformationLength, ReturnLength)


@Advapi32Proxy('SetTokenInformation')
def SetTokenInformation(TokenHandle, TokenInformationClass, TokenInformation, TokenInformationLength):
    return SetTokenInformation.ctypes_function(TokenHandle, TokenInformationClass, TokenInformation, TokenInformationLength)

@Advapi32Proxy('RegOpenKeyExA', should_return_zero_check)
def RegOpenKeyExA(hKey, lpSubKey, ulOptions, samDesired, phkResult):
    return RegOpenKeyExA.ctypes_function(hKey, lpSubKey, ulOptions, samDesired, phkResult)

# Security stuff

@Advapi32Proxy('GetNamedSecurityInfoA', should_return_zero_check)
def GetNamedSecurityInfoA(pObjectName, ObjectType, SecurityInfo, ppsidOwner=None, ppsidGroup=None, ppDacl=None, ppSacl=None, ppSecurityDescriptor=None):
    return GetNamedSecurityInfoA.ctypes_function(pObjectName, ObjectType, SecurityInfo, ppsidOwner, ppsidGroup, ppDacl, ppSacl, ppSecurityDescriptor)


@Advapi32Proxy('GetNamedSecurityInfoW', should_return_zero_check)
def GetNamedSecurityInfoW(pObjectName, ObjectType, SecurityInfo, ppsidOwner=None, ppsidGroup=None, ppDacl=None, ppSacl=None, ppSecurityDescriptor=None):
    return GetNamedSecurityInfoW.ctypes_function(pObjectName, ObjectType, SecurityInfo, ppsidOwner, ppsidGroup, ppDacl, ppSacl, ppSecurityDescriptor)


@Advapi32Proxy('GetSecurityInfo', should_return_zero_check)
def GetSecurityInfo(handle, ObjectType, SecurityInfo, ppsidOwner=None, ppsidGroup=None, ppDacl=None, ppSacl=None, ppSecurityDescriptor=None):
    return GetSecurityInfo.ctypes_function(handle, ObjectType, SecurityInfo, ppsidOwner, ppsidGroup, ppDacl, ppSacl, ppSecurityDescriptor)


# Sid stuff
@Advapi32Proxy('ConvertStringSidToSidA')
def ConvertStringSidToSidA(StringSid, Sid):
    return ConvertStringSidToSidA.ctypes_function(StringSid, Sid)

@Advapi32Proxy('ConvertStringSidToSidW')
def ConvertStringSidToSidW(StringSid, Sid):
    return ConvertStringSidToSidW.ctypes_function(StringSid, Sid)

@Advapi32Proxy('ConvertSidToStringSidA')
def ConvertSidToStringSidA(Sid, StringSid):
    return ConvertSidToStringSidA.ctypes_function(Sid, StringSid)

@Advapi32Proxy('ConvertSidToStringSidW')
def ConvertSidToStringSidW(Sid, StringSid):
    return ConvertSidToStringSidW.ctypes_function(Sid, StringSid)


    # Registry stuff

# TODO: default values? which ones ?

@Advapi32Proxy('RegOpenKeyExW', should_return_zero_check)
def RegOpenKeyExW(hKey, lpSubKey, ulOptions, samDesired, phkResult):
    return RegOpenKeyExW.ctypes_function(hKey, lpSubKey, ulOptions, samDesired, phkResult)


@Advapi32Proxy('RegGetValueA', should_return_zero_check)
def RegGetValueA(hkey, lpSubKey, lpValue, dwFlags, pdwType, pvData, pcbData):
    return RegGetValueA.ctypes_function(hkey, lpSubKey, lpValue, dwFlags, pdwType, pvData, pcbData)


@Advapi32Proxy('RegGetValueW', should_return_zero_check)
def RegGetValueW(hkey, lpSubKey=None, lpValue=NeededParameter, dwFlags=0, pdwType=None, pvData=None, pcbData=None):
    return RegGetValueW.ctypes_function(hkey, lpSubKey, lpValue, dwFlags, pdwType, pvData, pcbData)

@Advapi32Proxy('RegQueryValueExA', should_return_zero_check)
def RegQueryValueExA(hKey, lpValueName, lpReserved, lpType, lpData, lpcbData):
    return RegQueryValueExA.ctypes_function(hKey, lpValueName, lpReserved, lpType, lpData, lpcbData)

@Advapi32Proxy('RegQueryValueExW', should_return_zero_check)
def RegQueryValueExW(hKey, lpValueName, lpReserved, lpType, lpData, lpcbData):
    return RegQueryValueExA.ctypes_function(hKey, lpValueName, lpReserved, lpType, lpData, lpcbData)

@Advapi32Proxy('RegCloseKey', should_return_zero_check)
def RegCloseKey(hKey):
    return RegCloseKey.ctypes_function(hKey)


# Services
@Advapi32Proxy('OpenSCManagerA')
def OpenSCManagerA(lpMachineName=None, lpDatabaseName=None, dwDesiredAccess=SC_MANAGER_ALL_ACCESS):
    return OpenSCManagerA.ctypes_function(lpMachineName, lpDatabaseName, dwDesiredAccess)


@Advapi32Proxy('OpenSCManagerW')
def OpenSCManagerW(lpMachineName=None, lpDatabaseName=None, dwDesiredAccess=SC_MANAGER_ALL_ACCESS):
    return OpenSCManagerW.ctypes_function(lpMachineName, lpDatabaseName, dwDesiredAccess)


@Advapi32Proxy('EnumServicesStatusExA')
def EnumServicesStatusExA(hSCManager, InfoLevel, dwServiceType, dwServiceState, lpServices, cbBufSize, pcbBytesNeeded, lpServicesReturned, lpResumeHandle, pszGroupName):
    return EnumServicesStatusExA.ctypes_function(hSCManager, InfoLevel, dwServiceType, dwServiceState, lpServices, cbBufSize, pcbBytesNeeded, lpServicesReturned, lpResumeHandle, pszGroupName)


@Advapi32Proxy('EnumServicesStatusExW')
def EnumServicesStatusExW(hSCManager, InfoLevel, dwServiceType, dwServiceState, lpServices, cbBufSize, pcbBytesNeeded, lpServicesReturned, lpResumeHandle, pszGroupName):
    return EnumServicesStatusExW.ctypes_function(hSCManager, InfoLevel, dwServiceType, dwServiceState, lpServices, cbBufSize, pcbBytesNeeded, lpServicesReturned, lpResumeHandle, pszGroupName)


@Advapi32Proxy('StartServiceA')
def StartServiceA(hService, dwNumServiceArgs, lpServiceArgVectors):
    return StartServiceA.ctypes_function(hService, dwNumServiceArgs, lpServiceArgVectors)


@Advapi32Proxy('StartServiceW')
def StartServiceW(hService, dwNumServiceArgs, lpServiceArgVectors):
    return StartServiceW.ctypes_function(hService, dwNumServiceArgs, lpServiceArgVectors)


@Advapi32Proxy('OpenServiceA')
def OpenServiceA(hSCManager, lpServiceName, dwDesiredAccess):
    return OpenServiceA.ctypes_function(hSCManager, lpServiceName, dwDesiredAccess)


@Advapi32Proxy('OpenServiceW')
def OpenServiceW(hSCManager, lpServiceName, dwDesiredAccess):
    return OpenServiceW.ctypes_function(hSCManager, lpServiceName, dwDesiredAccess)


@Advapi32Proxy('CloseServiceHandle')
def CloseServiceHandle(hSCObject):
    return CloseServiceHandle.ctypes_function(hSCObject)

# Create process stuff

@Advapi32Proxy('CreateProcessAsUserA')
def CreateProcessAsUserA(hToken, lpApplicationName, lpCommandLine, lpProcessAttributes, lpThreadAttributes, bInheritHandles, dwCreationFlags, lpEnvironment, lpCurrentDirectory, lpStartupInfo, lpProcessInformation):
    return CreateProcessAsUserA.ctypes_function(hToken, lpApplicationName, lpCommandLine, lpProcessAttributes, lpThreadAttributes, bInheritHandles, dwCreationFlags, lpEnvironment, lpCurrentDirectory, lpStartupInfo, lpProcessInformation)

@Advapi32Proxy('CreateProcessAsUserW')
def CreateProcessAsUserW(hToken, lpApplicationName, lpCommandLine, lpProcessAttributes, lpThreadAttributes, bInheritHandles, dwCreationFlags, lpEnvironment, lpCurrentDirectory, lpStartupInfo, lpProcessInformation):
    return CreateProcessAsUserW.ctypes_function(hToken, lpApplicationName, lpCommandLine, lpProcessAttributes, lpThreadAttributes, bInheritHandles, dwCreationFlags, lpEnvironment, lpCurrentDirectory, lpStartupInfo, lpProcessInformation)


# Event log stuff

@Advapi32Proxy('OpenEventLogA')
def OpenEventLogA(lpUNCServerName=None, lpSourceName=NeededParameter):
    return OpenEventLogA.ctypes_function(lpUNCServerName, lpSourceName)

@Advapi32Proxy('OpenEventLogW')
def OpenEventLogW(lpUNCServerName=None, lpSourceName=NeededParameter):
    return OpenEventLogW.ctypes_function(lpUNCServerName, lpSourceName)

@Advapi32Proxy('ReadEventLogA')
def ReadEventLogA(hEventLog, dwReadFlags, dwRecordOffset, lpBuffer, nNumberOfBytesToRead, pnBytesRead, pnMinNumberOfBytesNeeded):
    return ReadEventLogA.ctypes_function(hEventLog, dwReadFlags, dwRecordOffset, lpBuffer, nNumberOfBytesToRead, pnBytesRead, pnMinNumberOfBytesNeeded)

@Advapi32Proxy('ReadEventLogW')
def ReadEventLogW(hEventLog, dwReadFlags, dwRecordOffset, lpBuffer, nNumberOfBytesToRead, pnBytesRead, pnMinNumberOfBytesNeeded):
    return ReadEventLogW.ctypes_function(hEventLog, dwReadFlags, dwRecordOffset, lpBuffer, nNumberOfBytesToRead, pnBytesRead, pnMinNumberOfBytesNeeded)

@Advapi32Proxy('GetEventLogInformation')
def GetEventLogInformation(hEventLog, dwInfoLevel, lpBuffer, cbBufSize, pcbBytesNeeded):
    return GetEventLogInformation.ctypes_function(hEventLog, dwInfoLevel, lpBuffer, cbBufSize, pcbBytesNeeded)

@Advapi32Proxy('GetNumberOfEventLogRecords')
def GetNumberOfEventLogRecords(hEventLog, NumberOfRecords):
    return GetNumberOfEventLogRecords.ctypes_function(hEventLog, NumberOfRecords)

@Advapi32Proxy('CloseEventLog')
def CloseEventLog(hEventLog):
    return CloseEventLog.ctypes_function(hEventLog)

# ##### Iphlpapi (network list and stuff) ###### #

def set_tcp_entry_error_check(func_name, result, func, args):
    """raise IphlpapiError if result is NOT 0 -- pretty print error 317"""
    if result:
        if result == 317:
            raise IphlpapiError(func_name, result, "<require elevated process>".format(func_name))
        raise IphlpapiError(func_name, result)
    return args

SetTcpEntry = TransparentIphlpapiProxy('SetTcpEntry', error_check=set_tcp_entry_error_check)


@IphlpapiProxy('GetExtendedTcpTable')
def GetExtendedTcpTable(pTcpTable, pdwSize=None, bOrder=True, ulAf=NeededParameter, TableClass=TCP_TABLE_OWNER_PID_ALL, Reserved=0):
    if pdwSize is None:
        pdwSize = ULONG(ctypes.sizeof(pTcpTable))
    return GetExtendedTcpTable.ctypes_function(pTcpTable, pdwSize, bOrder, ulAf, TableClass, Reserved)


@IphlpapiProxy('GetInterfaceInfo')
def GetInterfaceInfo(pIfTable, dwOutBufLen=None):
    if dwOutBufLen is None:
        dwOutBufLen = ULONG(ctypes.sizeof(pIfTable))
    return GetInterfaceInfo.ctypes_function(pIfTable, dwOutBufLen)


@IphlpapiProxy('GetIfTable')
def GetIfTable(pIfTable, pdwSize, bOrder=False):
    return GetIfTable.ctypes_function(pIfTable, pdwSize, bOrder)

@IphlpapiProxy('GetIpAddrTable')
def GetIpAddrTable(pIpAddrTable, pdwSize, bOrder=False):
    return GetIpAddrTable.ctypes_function(pIpAddrTable, pdwSize, bOrder)

# ## WinTrustProxy  PE signature##

@WinTrustProxy('WinVerifyTrust')
def WinVerifyTrust(hwnd, pgActionID, pWVTData):
    return WinVerifyTrust.ctypes_function(hwnd, pgActionID, pWVTData)


# ##Wintrust: catalog stuff ###

@WinTrustProxy('CryptCATAdminCalcHashFromFileHandle', error_check=zero_is_fail_error_check)
def CryptCATAdminCalcHashFromFileHandle(hFile, pcbHash, pbHash, dwFlags):
    return CryptCATAdminCalcHashFromFileHandle.ctypes_function(hFile, pcbHash, pbHash, dwFlags)


@WinTrustProxy('CryptCATAdminEnumCatalogFromHash')
def CryptCATAdminEnumCatalogFromHash(hCatAdmin, pbHash, cbHash, dwFlags, phPrevCatInfo):
    return CryptCATAdminEnumCatalogFromHash.ctypes_function(hCatAdmin, pbHash, cbHash, dwFlags, phPrevCatInfo)


@WinTrustProxy('CryptCATAdminAcquireContext', error_check=zero_is_fail_error_check)
def CryptCATAdminAcquireContext(phCatAdmin, pgSubsystem, dwFlags):
    return CryptCATAdminAcquireContext.ctypes_function(phCatAdmin, pgSubsystem, dwFlags)


@WinTrustProxy('CryptCATCatalogInfoFromContext', error_check=zero_is_fail_error_check)
def CryptCATCatalogInfoFromContext(hCatInfo, psCatInfo, dwFlags):
    return CryptCATCatalogInfoFromContext.ctypes_function(hCatInfo, psCatInfo, dwFlags)


@WinTrustProxy('CryptCATAdminReleaseCatalogContext')
def CryptCATAdminReleaseCatalogContext(hCatAdmin, hCatInfo, dwFlags):
    return CryptCATAdminReleaseCatalogContext.ctypes_function(hCatAdmin, hCatInfo, dwFlags)


@WinTrustProxy('CryptCATAdminReleaseContext')
def CryptCATAdminReleaseContext(hCatAdmin, dwFlags):
    return CryptCATAdminReleaseContext.ctypes_function(hCatAdmin, dwFlags)


@WinTrustProxy('CryptCATEnumerateAttr')
def CryptCATEnumerateAttr(hCatalog, pCatMember, pPrevAttr):
    return CryptCATEnumerateAttr.ctypes_function(hCatalog, pCatMember, pPrevAttr)


@WinTrustProxy('CryptCATEnumerateCatAttr')
def CryptCATEnumerateCatAttr(hCatalog, pPrevAttr):
    return CryptCATEnumerateCatAttr.ctypes_function(hCatalog, pPrevAttr)


@WinTrustProxy('CryptCATEnumerateMember')
def CryptCATEnumerateMember(hCatalog, pPrevMember):
    return CryptCATEnumerateMember.ctypes_function(hCatalog, pPrevMember)

## Crypto API ##
@Crypt32Proxy('CertStrToNameA')
def CertStrToNameA(dwCertEncodingType, pszX500, dwStrType, pvReserved, pbEncoded, pcbEncoded, ppszError):
    return CertStrToNameA.ctypes_function(dwCertEncodingType, pszX500, dwStrType, pvReserved, pbEncoded, pcbEncoded, ppszError)


@Crypt32Proxy('CertStrToNameW')
def CertStrToNameW(dwCertEncodingType, pszX500, dwStrType, pvReserved, pbEncoded, pcbEncoded, ppszError):
    return CertStrToNameW.ctypes_function(dwCertEncodingType, pszX500, dwStrType, pvReserved, pbEncoded, pcbEncoded, ppszError)


@Crypt32Proxy('CertCreateSelfSignCertificate')
def CertCreateSelfSignCertificate(hCryptProvOrNCryptKey, pSubjectIssuerBlob, dwFlags, pKeyProvInfo, pSignatureAlgorithm, pStartTime, pEndTime, pExtensions):
    return CertCreateSelfSignCertificate.ctypes_function(hCryptProvOrNCryptKey, pSubjectIssuerBlob, dwFlags, pKeyProvInfo, pSignatureAlgorithm, pStartTime, pEndTime, pExtensions)


@Crypt32Proxy('CertOpenStore')
def CertOpenStore(lpszStoreProvider, dwMsgAndCertEncodingType, hCryptProv, dwFlags, pvPara):
    if isinstance(lpszStoreProvider, (long, int)):
        lpszStoreProvider = LPCSTR(lpszStoreProvider)
    return CertOpenStore.ctypes_function(lpszStoreProvider, dwMsgAndCertEncodingType, hCryptProv, dwFlags, pvPara)


@Crypt32Proxy('CertAddCertificateContextToStore')
def CertAddCertificateContextToStore(hCertStore, pCertContext, dwAddDisposition, ppStoreContext):
    return CertAddCertificateContextToStore.ctypes_function(hCertStore, pCertContext, dwAddDisposition, ppStoreContext)


@Crypt32Proxy('PFXExportCertStoreEx')
def PFXExportCertStoreEx(hStore, pPFX, szPassword, pvPara, dwFlags):
    return PFXExportCertStoreEx.ctypes_function(hStore, pPFX, szPassword, pvPara, dwFlags)


@Advapi32Proxy('CryptGenKey')
def CryptGenKey(hProv, Algid, dwFlags, phKey):
    return CryptGenKey.ctypes_function(hProv, Algid, dwFlags, phKey)


@Advapi32Proxy('CryptDestroyKey')
def CryptDestroyKey(hKey):
    return CryptDestroyKey.ctypes_function(hKey)


@Advapi32Proxy('CryptAcquireContextA')
def CryptAcquireContextA(phProv, pszContainer, pszProvider, dwProvType, dwFlags):
    return CryptAcquireContextA.ctypes_function(phProv, pszContainer, pszProvider, dwProvType, dwFlags)


@Advapi32Proxy('CryptAcquireContextW')
def CryptAcquireContextW(phProv, pszContainer, pszProvider, dwProvType, dwFlags):
    return CryptAcquireContextW.ctypes_function(phProv, pszContainer, pszProvider, dwProvType, dwFlags)


@Advapi32Proxy('CryptReleaseContext')
def CryptReleaseContext(hProv, dwFlags):
    return CryptReleaseContext.ctypes_function(hProv, dwFlags)


@Advapi32Proxy('CryptExportKey')
def CryptExportKey(hKey, hExpKey, dwBlobType, dwFlags, pbData, pdwDataLen):
    return CryptExportKey.ctypes_function(hKey, hExpKey, dwBlobType, dwFlags, pbData, pdwDataLen)


@Crypt32Proxy('PFXImportCertStore')
def PFXImportCertStore(pPFX, szPassword, dwFlags):
    return PFXImportCertStore.ctypes_function(pPFX, szPassword, dwFlags)


@Crypt32Proxy('CertFindCertificateInStore')
def CertFindCertificateInStore(hCertStore, dwCertEncodingType, dwFindFlags, dwFindType, pvFindPara, pPrevCertContext):
    return CertFindCertificateInStore.ctypes_function(hCertStore, dwCertEncodingType, dwFindFlags, dwFindType, pvFindPara, pPrevCertContext)


@Crypt32Proxy('CertGetCertificateContextProperty')
def CertGetCertificateContextProperty(pCertContext, dwPropId, pvData, pcbData):
    return CertGetCertificateContextProperty.ctypes_function(pCertContext, dwPropId, pvData, pcbData)

@Crypt32Proxy('CertEnumCertificateContextProperties', no_error_check)
def CertEnumCertificateContextProperties(pCertContext, dwPropId):
    return CertEnumCertificateContextProperties.ctypes_function(pCertContext, dwPropId)


@Crypt32Proxy('CryptEncryptMessage')
def CryptEncryptMessage(pEncryptPara, cRecipientCert, rgpRecipientCert, pbToBeEncrypted, cbToBeEncrypted, pbEncryptedBlob, pcbEncryptedBlob):
    if isinstance(pbToBeEncrypted, basestring):
        # Transform string to array of byte
        pbToBeEncrypted = (BYTE * len(pbToBeEncrypted))(*bytearray(pbToBeEncrypted))
    if cbToBeEncrypted is None and pbToBeEncrypted is not None:
        cbToBeEncrypted = len(pbToBeEncrypted)
    return CryptEncryptMessage.ctypes_function(pEncryptPara, cRecipientCert, rgpRecipientCert, pbToBeEncrypted, cbToBeEncrypted, pbEncryptedBlob, pcbEncryptedBlob)


@Crypt32Proxy('CryptDecryptMessage')
def CryptDecryptMessage(pDecryptPara, pbEncryptedBlob, cbEncryptedBlob, pbDecrypted, pcbDecrypted, ppXchgCert):
    return CryptDecryptMessage.ctypes_function(pDecryptPara, pbEncryptedBlob, cbEncryptedBlob, pbDecrypted, pcbDecrypted, ppXchgCert)


@Crypt32Proxy('CryptAcquireCertificatePrivateKey')
def CryptAcquireCertificatePrivateKey(pCert, dwFlags, pvParameters, phCryptProvOrNCryptKey, pdwKeySpec, pfCallerFreeProvOrNCryptKey):
    return CryptAcquireCertificatePrivateKey.ctypes_function(pCert, dwFlags, pvParameters, phCryptProvOrNCryptKey, pdwKeySpec, pfCallerFreeProvOrNCryptKey)


@Crypt32Proxy('CertGetNameStringA')
def CertGetNameStringA(pCertContext, dwType, dwFlags, pvTypePara, pszNameString, cchNameString):
    return CertGetNameStringA.ctypes_function(pCertContext, dwType, dwFlags, pvTypePara, pszNameString, cchNameString)


@Crypt32Proxy('CertGetNameStringW')
def CertGetNameStringW(pCertContext, dwType, dwFlags, pvTypePara, pszNameString, cchNameString):
    return CertGetNameStringW.ctypes_function(pCertContext, dwType, dwFlags, pvTypePara, pszNameString, cchNameString)


@Crypt32Proxy('CertGetCertificateChain')
def CertGetCertificateChain(hChainEngine, pCertContext, pTime, hAdditionalStore, pChainPara, dwFlags, pvReserved, ppChainContext):
    return CertGetCertificateChain.ctypes_function(hChainEngine, pCertContext, pTime, hAdditionalStore, pChainPara, dwFlags, pvReserved, ppChainContext)

@Crypt32Proxy('CertDuplicateCertificateContext')
def CertDuplicateCertificateContext(pCertContext):
    return CertDuplicateCertificateContext.ctypes_function(pCertContext)


@Crypt32Proxy('CertEnumCertificatesInStore')
def CertEnumCertificatesInStore(hCertStore, pPrevCertContext):
    return CertEnumCertificatesInStore.ctypes_function(hCertStore, pPrevCertContext)

@Crypt32Proxy('CertCompareCertificate', error_check=no_error_check)
def CertCompareCertificate(dwCertEncodingType, pCertId1, pCertId2):
    """This function does not raise is compare has failed:
        return 0 if cert are NOT equals
    """
    return CertCompareCertificate.ctypes_function(dwCertEncodingType, pCertId1, pCertId2)

@Crypt32Proxy('CertEnumCTLsInStore')
def CertEnumCTLsInStore(hCertStore, pPrevCtlContext):
    return CertEnumCTLsInStore.ctypes_function(hCertStore, pPrevCtlContext)


@Crypt32Proxy('CryptEncodeObjectEx')
def CryptEncodeObjectEx(dwCertEncodingType, lpszStructType, pvStructInfo, dwFlags, pEncodePara, pvEncoded, pcbEncoded):
    lpszStructType = LPCSTR(lpszStructType) if isinstance(lpszStructType, (int, long)) else lpszStructType
    return CryptEncodeObjectEx.ctypes_function(dwCertEncodingType, lpszStructType, pvStructInfo, dwFlags, pEncodePara, pvEncoded, pcbEncoded)

@Crypt32Proxy('CertCreateCertificateContext')
def CertCreateCertificateContext(dwCertEncodingType, pbCertEncoded, cbCertEncoded):
    return CertCreateCertificateContext.ctypes_function(dwCertEncodingType, pbCertEncoded, cbCertEncoded)


@Crypt32Proxy('CryptQueryObject')
def CryptQueryObject(dwObjectType, pvObject, dwExpectedContentTypeFlags, dwExpectedFormatTypeFlags, dwFlags, pdwMsgAndCertEncodingType, pdwContentType, pdwFormatType, phCertStore, phMsg, ppvContext):
    return CryptQueryObject.ctypes_function(dwObjectType, pvObject, dwExpectedContentTypeFlags, dwExpectedFormatTypeFlags, dwFlags, pdwMsgAndCertEncodingType, pdwContentType, pdwFormatType, phCertStore, phMsg, ppvContext)


@Crypt32Proxy('CryptMsgGetParam')
def CryptMsgGetParam(hCryptMsg, dwParamType, dwIndex, pvData, pcbData):
    return CryptMsgGetParam.ctypes_function(hCryptMsg, dwParamType, dwIndex, pvData, pcbData)


@Crypt32Proxy('CryptDecodeObject')
def CryptDecodeObject(dwCertEncodingType, lpszStructType, pbEncoded, cbEncoded, dwFlags, pvStructInfo, pcbStructInfo):
    return CryptDecodeObject.ctypes_function(dwCertEncodingType, lpszStructType, pbEncoded, cbEncoded, dwFlags, pvStructInfo, pcbStructInfo)


@Crypt32Proxy('CryptMsgVerifyCountersignatureEncoded')
def CryptMsgVerifyCountersignatureEncoded(hCryptProv, dwEncodingType, pbSignerInfo, cbSignerInfo, pbSignerInfoCountersignature, cbSignerInfoCountersignature, pciCountersigner):
    return CryptMsgVerifyCountersignatureEncoded.ctypes_function(hCryptProv, dwEncodingType, pbSignerInfo, cbSignerInfo, pbSignerInfoCountersignature, cbSignerInfoCountersignature, pciCountersigner)


@Crypt32Proxy('CryptMsgVerifyCountersignatureEncodedEx')
def CryptMsgVerifyCountersignatureEncodedEx(hCryptProv, dwEncodingType, pbSignerInfo, cbSignerInfo, pbSignerInfoCountersignature, cbSignerInfoCountersignature, dwSignerType, pvSigner, dwFlags, pvExtra):
    return CryptMsgVerifyCountersignatureEncodedEx.ctypes_function(hCryptProv, dwEncodingType, pbSignerInfo, cbSignerInfo, pbSignerInfoCountersignature, cbSignerInfoCountersignature, dwSignerType, pvSigner, dwFlags, pvExtra)


@Crypt32Proxy('CryptHashCertificate')
def CryptHashCertificate(hCryptProv, Algid, dwFlags, pbEncoded, cbEncoded, pbComputedHash, pcbComputedHash):
   return CryptHashCertificate.ctypes_function(hCryptProv, Algid, dwFlags, pbEncoded, cbEncoded, pbComputedHash, pcbComputedHash)

# ## CryptUI ## #

@CryptUIProxy('CryptUIDlgViewContext')
def CryptUIDlgViewContext(dwContextType, pvContext, hwnd, pwszTitle, dwFlags, pvReserved):
    return CryptUIDlgViewContext.ctypes_function(dwContextType, pvContext, hwnd, pwszTitle, dwFlags, pvReserved)

# ## User32 stuff ## #

EnumWindows = TransparentUser32Proxy('EnumWindows')
EnumChildWindows = TransparentUser32Proxy('EnumChildWindows')
GetParent = TransparentUser32Proxy('GetParent')
GetClassInfoExA = TransparentUser32Proxy('GetClassInfoExA')
GetClassInfoExW = TransparentUser32Proxy('GetClassInfoExW')
GetWindowTextA = TransparentUser32Proxy('GetWindowTextA', no_error_check)
GetWindowTextW = TransparentUser32Proxy('GetWindowTextW', no_error_check)
GetWindowModuleFileNameA = TransparentUser32Proxy('GetWindowModuleFileNameA', no_error_check)
GetWindowModuleFileNameW = TransparentUser32Proxy('GetWindowModuleFileNameW', no_error_check)
GetSystemMetrics = TransparentUser32Proxy('GetSystemMetrics', no_error_check)
GetWindowThreadProcessId = TransparentUser32Proxy('GetWindowThreadProcessId')

@User32Proxy("GetCursorPos")
def GetCursorPos(lpPoint):
    return GetCursorPos.ctypes_function(lpPoint)


@User32Proxy("WindowFromPoint")
def WindowFromPoint(Point):
    return WindowFromPoint.ctypes_function(Point)

@User32Proxy("GetWindowRect")
def GetWindowRect(hWnd, lpRect):
    return GetWindowRect.ctypes_function(hWnd, lpRect)

@User32Proxy("MessageBoxA")
def MessageBoxA(hWnd=0, lpText=NeededParameter, lpCaption=None, uType=0):
    return MessageBoxA.ctypes_function(hWnd, lpText, lpCaption, uType)

@User32Proxy("MessageBoxW")
def MessageBoxW(hWnd=0, lpText=NeededParameter, lpCaption=None, uType=0):
    return MessageBoxW.ctypes_function(hWnd, lpText, lpCaption, uType)

@User32Proxy("RealGetWindowClassA")
def RealGetWindowClassA(hwnd, pszType, cchType=None):
    if cchType is None:
        cchType = len(pszType)
    return RealGetWindowClassA.ctypes_function(hwnd, pszType, cchType)

@User32Proxy("RealGetWindowClassW")
def RealGetWindowClassW(hwnd, pszType, cchType=None):
    if cchType is None:
        cchType = len(pszType)
    return RealGetWindowClassW.ctypes_function(hwnd, pszType, cchType)

@User32Proxy("GetClassNameA")
def GetClassNameA (hwnd, pszType, cchType=None):
    if cchType is None:
        cchType = len(pszType)
    return GetClassNameA .ctypes_function(hwnd, pszType, cchType)

@User32Proxy("GetClassNameW")
def GetClassNameW (hwnd, pszType, cchType=None):
    if cchType is None:
        cchType = len(pszType)
    return GetClassNameW .ctypes_function(hwnd, pszType, cchType)

# ## Version stuff ## #

@VersionProxy("GetFileVersionInfoA")
def GetFileVersionInfoA(lptstrFilename, dwHandle=0, dwLen=None, lpData=NeededParameter):
    if dwLen is None and lpData is not None:
        dwLen = len(lpData)
    return GetFileVersionInfoA.ctypes_function(lptstrFilename, dwHandle, dwLen, lpData)


@VersionProxy("GetFileVersionInfoW")
def GetFileVersionInfoW(lptstrFilename, dwHandle=0, dwLen=None, lpData=NeededParameter):
    if dwLen is None and lpData is not None:
        dwLen = len(lpData)
    return GetFileVersionInfoA.ctypes_function(lptstrFilename, dwHandle, dwLen, lpData)


@VersionProxy("GetFileVersionInfoSizeA")
def GetFileVersionInfoSizeA(lptstrFilename, lpdwHandle=None):
    if lpdwHandle is None:
        lpdwHandle = ctypes.byref(DWORD())
    return GetFileVersionInfoSizeA.ctypes_function(lptstrFilename, lpdwHandle)


@VersionProxy("GetFileVersionInfoSizeW")
def GetFileVersionInfoSizeW(lptstrFilename, lpdwHandle=None):
    if lpdwHandle is None:
        lpdwHandle = ctypes.byref(DWORD())
    return GetFileVersionInfoSizeW.ctypes_function(lptstrFilename, lpdwHandle)


@VersionProxy("VerQueryValueA")
def VerQueryValueA(pBlock, lpSubBlock, lplpBuffer, puLen):
    return VerQueryValueA.ctypes_function(pBlock, lpSubBlock, lplpBuffer, puLen)


@VersionProxy("VerQueryValueW")
def VerQueryValueW(pBlock, lpSubBlock, lplpBuffer, puLen):
    return VerQueryValueW.ctypes_function(pBlock, lpSubBlock, lplpBuffer, puLen)


# ## Ole32Proxy (COM STUFF) ## #

@Ole32Proxy('CoInitializeEx', no_error_check)
def CoInitializeEx(pvReserved=None, dwCoInit=COINIT_MULTITHREADED):
    return CoInitializeEx.ctypes_function(pvReserved, dwCoInit)


@Ole32Proxy('CoInitializeSecurity')
def CoInitializeSecurity(pSecDesc, cAuthSvc, asAuthSvc, pReserved1, dwAuthnLevel, dwImpLevel, pAuthList, dwCapabilities, pReserved3):
    return CoInitializeSecurity.ctypes_function(pSecDesc, cAuthSvc, asAuthSvc, pReserved1, dwAuthnLevel, dwImpLevel, pAuthList, dwCapabilities, pReserved3)


@Ole32Proxy('CoCreateInstance')
def CoCreateInstance(rclsid, pUnkOuter=None, dwClsContext=CLSCTX_INPROC_SERVER, riid=NeededParameter, ppv=NeededParameter):
    return CoCreateInstance.ctypes_function(rclsid, pUnkOuter, dwClsContext, riid, ppv)

# ## Shell32 ## #

@Shell32Proxy('ShellExecuteA')
def ShellExecuteA(hwnd, lpOperation, lpFile, lpParameters, lpDirectory, nShowCmd):
    return ShellExecuteA.ctypes_function(hwnd, lpOperation, lpFile, lpParameters, lpDirectory, nShowCmd)

@Shell32Proxy('ShellExecuteW')
def ShellExecuteW(hwnd, lpOperation, lpFile, lpParameters, lpDirectory, nShowCmd):
    return ShellExecuteW.ctypes_function(hwnd, lpOperation, lpFile, lpParameters, lpDirectory, nShowCmd)


# Transactions #

@Kernel32Proxy("CreateFileTransactedA", error_check=valid_handle_check)
def CreateFileTransactedA(lpFileName, dwDesiredAccess, dwShareMode, lpSecurityAttributes, dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile, hTransaction, pusMiniVersion, pExtendedParameter):
    return CreateFileTransactedA.ctypes_function(lpFileName, dwDesiredAccess, dwShareMode, lpSecurityAttributes, dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile, hTransaction, pusMiniVersion, pExtendedParameter)

@Kernel32Proxy("CreateFileTransactedW", error_check=valid_handle_check)
def CreateFileTransactedW(lpFileName, dwDesiredAccess, dwShareMode, lpSecurityAttributes, dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile, hTransaction, pusMiniVersion, pExtendedParameter):
    return CreateFileTransactedW.ctypes_function(lpFileName, dwDesiredAccess, dwShareMode, lpSecurityAttributes, dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile, hTransaction, pusMiniVersion, pExtendedParameter)


@Ktmw32Proxy("CommitTransaction")
def CommitTransaction(TransactionHandle):
    return CommitTransaction.ctypes_function(TransactionHandle)


@Ktmw32Proxy("CreateTransaction")
def CreateTransaction(lpTransactionAttributes, UOW, CreateOptions, IsolationLevel, IsolationFlags, Timeout, Description):
    return CreateTransaction.ctypes_function(lpTransactionAttributes, UOW, CreateOptions, IsolationLevel, IsolationFlags, Timeout, Description)


@Ktmw32Proxy("RollbackTransaction")
def RollbackTransaction(TransactionHandle):
    return RollbackTransaction.ctypes_function(TransactionHandle)


@Ktmw32Proxy("OpenTransaction")
def OpenTransaction(dwDesiredAccess, TransactionId):
    return OpenTransaction.ctypes_function(dwDesiredAccess, TransactionId)


# Pipe

@Kernel32Proxy("CreateNamedPipeA")
def CreateNamedPipeA(lpName, dwOpenMode, dwPipeMode, nMaxInstances, nOutBufferSize, nInBufferSize, nDefaultTimeOut, lpSecurityAttributes):
    return CreateNamedPipeA.ctypes_function(lpName, dwOpenMode, dwPipeMode, nMaxInstances, nOutBufferSize, nInBufferSize, nDefaultTimeOut, lpSecurityAttributes)

@Kernel32Proxy("CreateNamedPipeW")
def CreateNamedPipeW(lpName, dwOpenMode, dwPipeMode, nMaxInstances, nOutBufferSize, nInBufferSize, nDefaultTimeOut, lpSecurityAttributes):
    return CreateNamedPipeW.ctypes_function(lpName, dwOpenMode, dwPipeMode, nMaxInstances, nOutBufferSize, nInBufferSize, nDefaultTimeOut, lpSecurityAttributes)

@Kernel32Proxy("ConnectNamedPipe")
def ConnectNamedPipe(hNamedPipe, lpOverlapped):
    return ConnectNamedPipe.ctypes_function(hNamedPipe, lpOverlapped)

@Kernel32Proxy("SetNamedPipeHandleState")
def SetNamedPipeHandleState(hNamedPipe, lpMode, lpMaxCollectionCount, lpCollectDataTimeout):
    return SetNamedPipeHandleState.ctypes_function(hNamedPipe, lpMode, lpMaxCollectionCount, lpCollectDataTimeout)

