"""GitHub 审阅助手的内部实现。

本包只放**不带装饰器**的逻辑：指令、钩子、定时任务的注册全部留在插件主模块
``main.py``（AstrBot 用 ``star_map[handler.handler_module_path]`` 索引元数据，
装饰器放到子模块会让钩子静默失效）。
"""
