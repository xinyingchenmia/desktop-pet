"""
py2app build script.
Build:  python3 setup.py py2app
Output: dist/Desktop Pet.app
"""
from setuptools import setup

APP = ['pet.py']

DATA_FILES = [
    'house.PNG',
    'food.png',
    'food_clean.png',
    'cat_rest.png',
    'cat_walk1.png',
    'cat_walk2.png',
    'cat_walk3.png',
]

OPTIONS = {
    'argv_emulation': False,
    'packages': ['pynput', 'PIL'],
    'includes': [
        'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets',
        'Quartz', 'AppKit', 'objc',
    ],
    'excludes': [
        'rembg', 'onnxruntime', 'torch', 'tensorflow', 'transformers',
        'scipy', 'numba', 'pymatting', 'matplotlib', 'pandas', 'sklearn',
        'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtMultimedia',
        'PyQt6.QtNetwork', 'PyQt6.QtSql', 'PyQt6.QtBluetooth', 'PyQt6.QtNfc',
        'PyQt6.QtPositioning', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort',
        'PyQt6.QtTest', 'PyQt6.QtDesigner', 'PyQt6.QtHelp', 'PyQt6.QtPdf',
        'PyQt6.QtQuick', 'PyQt6.QtQml', 'PyQt6.Qt3DCore', 'PyQt6.QtCharts',
        'PyQt6.QtDataVisualization', 'PyQt6.QtSvg', 'PyQt6.QtOpenGL',
        'PyQt6.QtOpenGLWidgets', 'PyQt6.QtPrintSupport', 'PyQt6.QtDBus',
    ],
    'plist': {
        'CFBundleName': 'Desktop Pet',
        'CFBundleDisplayName': 'Desktop Pet',
        'CFBundleIdentifier': 'com.miffy.desktoppet',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': True,
        'NSAppleEventsUsageDescription':
            'Desktop Pet needs to detect which window is currently active.',
    },
}

setup(
    app=APP,
    name='Desktop Pet',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
