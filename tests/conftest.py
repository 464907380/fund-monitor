"""pytest 共享配置"""
import sys
import os

# 将 src 目录加入 Python 路径，使测试文件可以直接 import 各模块
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
