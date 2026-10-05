try:
    from .jsonlfix import main
except ImportError:  # 直接 python jsonlfix.py / python -m jsonlfix 兜底
    from jsonlfix import main

if __name__ == "__main__":
    raise SystemExit(main())
