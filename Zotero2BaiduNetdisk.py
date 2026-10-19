"""

将Zotero中的PDF等附件按照文库结构整理成文件夹，可设置成百度网盘自动备份文件夹进行同步。

用法：
    python zotero_attach_sync.py              # 执行迁移
    python zotero_attach_sync.py --dry-run     # 预览模式，不实际修改
    python zotero_attach_sync.py --status      # 仅查看状态

工作流：
    1. 读取Zotero数据库，获取所有附件及其所属集合
    2. 按集合层级创建子文件夹
    3. 将附件从storage目录复制到目标目录的对应集合文件夹
    4. 更新数据库中的附件路径为绝对路径
    5. 输出迁移统计
"""

import sqlite3
import os
import shutil
import argparse
import sys
from datetime import datetime


# ============ 配置区 ============
ZOTERO_DATA_DIR = r"D:\RrJm\Zotero"          # Zotero数据目录
SYNC_TARGET_DIR = r"D:\RrJm\Zotero-PDFs"     # 同步目标目录
# ================================


def get_collection_tree(cursor):
    """获取集合层级树，返回 {collectionID: {'name': str, 'parent': int}}"""
    cursor.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections")
    return {row[0]: {'name': row[1], 'parent': row[2]} for row in cursor.fetchall()}


def get_collection_full_path(collections, collection_id):
    """根据 collectionID 获取完整路径（如 Biofilm/EradicationAgents）"""
    if not collection_id:
        return "未分类"
    parts = []
    cid = collection_id
    while cid and cid in collections:
        parts.insert(0, collections[cid]['name'])
        cid = collections[cid]['parent']
    return os.path.join(*parts) if parts else "未分类"


def find_file_in_storage(storage_dir, filename):
    """在 storage 目录的哈希子文件夹中查找文件"""
    for subdir in os.listdir(storage_dir):
        subdir_path = os.path.join(storage_dir, subdir)
        if not os.path.isdir(subdir_path):
            continue
        candidate = os.path.join(subdir_path, filename)
        if os.path.exists(candidate):
            return candidate
    return None


def get_attachment_list(cursor):
    """获取所有附件及其集合归属"""
    cursor.execute("""
        SELECT ia.itemID, ia.path, ia.linkMode, ci.collectionID
        FROM itemAttachments ia
        LEFT JOIN collectionItems ci ON ia.parentItemID = ci.itemID
        WHERE ia.path IS NOT NULL
    """)
    return cursor.fetchall()


def status_report(db_path, storage_dir, target_dir):
    """输出当前状态统计"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # 按 linkMode 统计
    c.execute("SELECT linkMode, COUNT(*) FROM itemAttachments GROUP BY linkMode")
    linkmode_map = {0: "imported_file (原生存储)", 1: "linked_file (外部链接)", 2: "imported_url", 3: "embedded_image"}
    print("=" * 50)
    print("Zotero 附件状态")
    print("=" * 50)
    for row in c.fetchall():
        lm, cnt = row
        name = linkmode_map.get(lm, f"unknown({lm})")
        print(f"  {name}: {cnt} 个")

    # 统计还在 storage 里的文件
    c.execute("SELECT COUNT(*) FROM itemAttachments WHERE path LIKE 'storage:%'")
    still_in_storage = c.fetchone()[0]
    print(f"\n  仍在原生 storage 中: {still_in_storage} 个")

    # 统计目标目录文件数
    target_files = sum(len(files) for _, _, files in os.walk(target_dir)) if os.path.exists(target_dir) else 0
    print(f"  同步目录文件数: {target_files} 个")

    conn.close()


def migrate(db_path, storage_dir, target_dir, dry_run=False):
    """执行迁移"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    collections = get_collection_tree(c)
    attachments = get_attachment_list(c)

    # 筛选需要迁移的：path 以 storage: 开头的
    to_migrate = []
    for item_id, path, link_mode, col_id in attachments:
        if path and path.startswith("storage:"):
            to_migrate.append((item_id, path, col_id))

    if not to_migrate:
        print("✓ 所有附件已完成迁移，无需操作")
        conn.close()
        return

    print(f"发现 {len(to_migrate)} 个附件待迁移")
    print(f"目标目录: {target_dir}")
    print(f"预览模式: {'是（不实际修改）' if dry_run else '否'}")
    print("-" * 50)

    migrated = 0
    skipped = 0
    errors = 0

    for item_id, path, col_id in to_migrate:
        filename = path.replace("storage:", "")
        collection_path = get_collection_full_path(collections, col_id)
        target_subdir = os.path.join(target_dir, collection_path)
        target_file = os.path.join(target_subdir, filename)

        # 在 storage 里找源文件
        src_file = find_file_in_storage(storage_dir, filename)
        if not src_file:
            print(f"  ✗ itemID={item_id}: 未找到文件 {filename[:50]}")
            errors += 1
            continue

        # 文件已存在于目标位置
        if os.path.exists(target_file):
            print(f"  ⊙ 已存在: [{collection_path}] {filename[:45]}...")
            skipped += 1
            # 仍然更新数据库路径
            if not dry_run:
                new_path = target_file.replace("\\", "/")
                c.execute("UPDATE itemAttachments SET path=? WHERE itemID=?", (new_path, item_id))
            continue

        # 执行迁移
        if dry_run:
            print(f"  → [{collection_path}] {filename[:45]}...")
        else:
            os.makedirs(target_subdir, exist_ok=True)
            shutil.copy2(src_file, target_file)
            new_path = target_file.replace("\\", "/")
            c.execute("UPDATE itemAttachments SET path=? WHERE itemID=?", (new_path, item_id))
            print(f"  ✓ [{collection_path}] {filename[:45]}...")

        migrated += 1

    if not dry_run:
        conn.commit()

    conn.close()

    print("-" * 50)
    print(f"迁移完成: {migrated} 个新迁移, {skipped} 个已存在, {errors} 个错误")


def check_zotero_running():
    """检测 Zotero 是否正在运行"""
    try:
        import subprocess
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
            capture_output=True, text=True, timeout=5
        )
        return "zotero.exe" in result.stdout.lower()
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Zotero 附件结构化同步工具")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际修改")
    parser.add_argument("--status", action="store_true", help="仅查看状态")
    parser.add_argument("--data-dir", default=ZOTERO_DATA_DIR, help="Zotero 数据目录")
    parser.add_argument("--target-dir", default=SYNC_TARGET_DIR, help="同步目标目录")
    args = parser.parse_args()

    db_path = os.path.join(args.data_dir, "zotero.sqlite")
    storage_dir = os.path.join(args.data_dir, "storage")

    if not os.path.exists(db_path):
        print(f"错误: 找不到 Zotero 数据库 {db_path}")
        sys.exit(1)

    # 检测 Zotero 是否在运行
    if check_zotero_running():
        print("=" * 50)
        print("错误: Zotero 正在运行，数据库被锁定")
        print("=" * 50)
        print("请先关闭 Zotero，再运行此脚本。")
        print("关闭方式: 右键任务栏 Zotero 图标 → 退出")
        sys.exit(1)

    if args.status:
        status_report(db_path, storage_dir, args.target_dir)
        return

    # 操作前备份数据库
    backup_path = db_path + ".backup-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    if not args.dry_run:
        shutil.copy2(db_path, backup_path)
        print(f"已备份数据库到: {backup_path}\n")

    migrate(db_path, storage_dir, args.target_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
