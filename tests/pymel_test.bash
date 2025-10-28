#!/bin/bash

this_script=$(python -c "import os; import posixpath; print(os.path.normcase(os.path.normpath(os.path.realpath(os.path.abspath('''$0''')))).replace(os.sep, posixpath.sep))")
echo "$this_script"

if [[ "${MAYA_PYTHON_VERSION}" == 2 ]]; then
    mayapy_name=mayapy2
else
    mayapy_name=mayapy
fi

print_python_module_dir() {
    module="$1"
    $mayapy_name -c '
try:
    import '$module' as test_mod
except ImportError:
    # print nothing
    pass
else:
    import os
    import inspect
    mod_path = inspect.getsourcefile(test_mod)
    mod_dir = os.path.dirname(mod_path)
    if os.path.basename(os.path.splitext(mod_path)[0]) == "__init__":
        mod_dir = os.path.dirname(mod_dir)
    print(mod_dir)
'
}

this_dir=$(dirname "$this_script")
echo "$this_dir"
pymel_dir=$(dirname "$this_dir")

pytest_dir=$(print_python_module_dir pytest)
pkg_resources_dir=$(print_python_module_dir pkg_resources)

mayapy_exc=$(which ${mayapy_name})
mayapy_dir=$(dirname "${mayapy_exc}")

the_cmd=("${mayapy_exc}"
    "${this_dir}/pymel_test.py" --gui-stdout "$@")
env_vars=(DISPLAY=:0.0 "HOME=$HOME" "TERM=$TERM" "SHELL=$SHELL"
    "USER=$USER" "PATH=$PATH:$mayapy_dir"
    "PYTHONPATH=$pymel_dir:$pytest_dir:$pkg_resources_dir")
echo "env_vars:"
echo "${env_vars[@]}"
echo "the_cmd:"
echo "${the_cmd[@]}"
env -i "${env_vars[@]}" "${the_cmd[@]}"
