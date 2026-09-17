#!/usr/bin/env python3
"""Crawl compliant (non-violation) workplace-safety image datasets.

These images serve as NEGATIVE samples for the binary classifiers:
- normal_work (no phone call, no smoking) → negative for phone_call/smoking
- wearing_reflective_vest → negative for no_reflective_vest
- forklift_with_helmet → negative for forklift_no_helmet
- normal_phone_use (browsing/texting, not calling) → negative for mobile_use
- clean_workspace (no items left) → negative for items_left
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path


SCENES: dict[str, dict[str, object]] = {
    "normal_work": {
        "label": "正常工作",
        "description": "工人正常工作，不打电话、不吸烟",
        "negative_for": ["phone_call", "smoking"],
        "keywords": [
            "工人正常工作", "工厂工人工作", "车间工人作业", "仓库员工工作",
            "物流工人装卸", "施工现场工人", "建筑工人施工", "生产线工人",
            "装配线工人", "操作工正常作业", "工人专注工作", "员工认真工作",
            "工厂车间生产", "仓库理货员", "物流分拣员", "工地施工人员",
            "worker working normally", "factory worker at work", "warehouse employee working",
            "construction worker working", "industrial worker on duty", "operator working",
            "assembly line worker", "production line worker", "worker focused on task",
            "工厂员工正常作业", "车间操作工", "仓库管理员工作", "物流员工作",
            "施工人员作业", "建筑工施工", "生产工人", "制造工人",
            "worker on production line", "factory employee working", "warehouse worker picking",
            "construction worker on site", "industrial worker operating machinery",
            "正常工作的工人", "认真工作的员工", "专注工作的工人", "岗位工人",
            "factory worker focused", "warehouse worker scanning", "logistics worker sorting",
            "construction worker building", "manufacturing worker assembling",
        ],
    },
    "wearing_reflective_vest": {
        "label": "穿反光背心",
        "description": "工人穿着反光背心/安全背心",
        "negative_for": ["no_reflective_vest"],
        "keywords": [
            "穿反光背心的工人", "反光背心工人", "安全背心工人", "穿反光衣的工人",
            "工地反光背心", "施工反光背心", "仓库反光背心", "物流反光背心",
            "工厂安全背心", "车间反光背心", "环卫工人反光背心", "交警反光背心",
            "反光安全服工人", "穿反光背心施工", "穿反光背心作业", "反光背心工作服",
            "worker wearing reflective vest", "safety vest worker", "hi-vis vest worker",
            "construction worker reflective vest", "warehouse worker safety vest",
            "industrial worker hi-vis", "traffic worker reflective vest",
            "穿反光衣施工人员", "反光背心工作服", "安全反光背心", "荧光背心工人",
            "factory worker in safety vest", "warehouse employee in hi-vis",
            "logistics worker reflective", "construction worker safety vest",
            "反光背心的建筑工人", "穿反光背心的仓库管理员", "穿安全背心的工人",
            "worker in high visibility vest", "employee wearing safety vest",
            "反光背心现场施工", "穿反光背心的物流员", "穿反光衣的工厂工人",
        ],
    },
    "forklift_with_helmet": {
        "label": "叉车戴安全帽",
        "description": "叉车司机戴着安全帽驾驶叉车",
        "negative_for": ["forklift_no_helmet"],
        "keywords": [
            "叉车司机戴安全帽", "戴安全帽开叉车", "叉车工人安全帽", "仓库叉车司机",
            "工厂叉车作业", "叉车驾驶员", "戴头盔开叉车", "叉车操作员安全帽",
            "物流园叉车", "仓库叉车装卸", "工厂叉车搬运", "叉车安全作业",
            "forklift driver wearing helmet", "forklift operator safety helmet",
            "warehouse forklift driver", "factory forklift operator",
            "forklift worker hard hat", "forklift driver with hardhat",
            "戴安全帽的叉车司机", "叉车司机安全头盔", "叉车作业戴安全帽",
            "仓库叉车工", "叉车装卸工", "叉车驾驶员安全帽", "工厂叉车工",
            "forklift driver in warehouse", "forklift operator in factory",
            "warehouse forklift safety", "forklift driver hard hat",
            "开叉车的工人戴安全帽", "叉车司机佩戴安全帽", "叉车作业人员安全帽",
            "forklift driver wearing hard hat", "forklift operator with helmet",
            "叉车司机安全作业", "戴头盔的叉车工人", "叉车驾驶员戴安全帽",
        ],
    },
    "normal_phone_use": {
        "label": "正常使用手机",
        "description": "正常使用手机（浏览、打字、看视频），不是打电话",
        "negative_for": ["mobile_use"],
        "keywords": [
            "正常使用手机", "看手机", "玩手机", "浏览手机", "打字发短信",
            "看手机视频", "刷手机", "用手机拍照", "手机购物", "手机导航",
            "worker looking at phone", "employee using smartphone", "worker texting",
            "worker browsing phone", "person using mobile phone", "people on smartphones",
            "看手机的人", "玩手机的人", "低头看手机", "用手机打字",
            "手机上网", "手机看新闻", "手机支付", "手机扫码",
            "person texting on phone", "person browsing smartphone", "people using phones",
            "worker checking phone", "employee on mobile phone", "using phone for work",
            "正常看手机", "工作间隙看手机", "休息时玩手机", "用手机查资料",
            "worker using phone for work", "employee checking messages", "person scrolling phone",
            "手机办公", "手机沟通", "手机回复消息", "手机查看信息",
        ],
    },
    "clean_workspace": {
        "label": "整洁工作场景",
        "description": "整洁的工作场景，没有物品遗留",
        "negative_for": ["items_left"],
        "keywords": [
            "整洁的工厂", "整洁的车间", "整洁的仓库", "整洁的工作环境",
            "干净的生产线", "整洁的施工场地", "有序的仓库", "5S管理工厂",
            "clean factory", "clean warehouse", "tidy workshop", "organized workspace",
            "clean production line", "well organized warehouse", "5S factory",
            "工厂整洁有序", "车间干净整洁", "仓库管理规范", "工作环境整洁",
            "clean industrial workspace", "tidy manufacturing floor", "organized warehouse",
            "整洁的物流仓库", "干净的施工场地", "有序的生产车间", "规范的工作现场",
            "factory floor clean", "warehouse aisle clean", "workplace tidy",
            "没有杂物的工厂", "整洁的作业区域", "干净的工作区域", "有序的物料摆放",
            "clean work area", "organized material storage", "tidy production area",
            "工厂5S现场", "车间定置管理", "仓库目视化管理", "整洁的物流中心",
        ],
    },
}


def image_count(output_dir: Path) -> int:
    return sum(1 for _ in output_dir.glob("batch_*/*.jpg"))


def build_command(
    scene: dict[str, object],
    output_dir: Path,
    total: int,
    rate: float,
    workers: int,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "smart_spider.dataset_cli",
        "--keywords",
        ",".join(scene["keywords"]),
        "--labels",
        str(scene["label"]),
        "--label-mode",
        "fixed",
        "--total",
        str(total),
        "--output",
        str(output_dir),
        "--engines",
        str(scene.get("engines", "baidu,bing")),
        "--no-clip",
        "--image-output-format",
        "jpg",
        "--batch-size",
        "100",
        "--rate",
        str(rate),
        "--max-workers",
        str(workers),
        "--timeout",
        "12",
        "--max-retries",
        "1",
        "--max-file-size",
        str(12 * 1024 * 1024),
        "--no-curl-cffi",
        "--resume",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", default="dataset_compliant_scenes_20260913")
    parser.add_argument("--total", type=int, default=5000)
    parser.add_argument("--parallel", type=int, default=5)
    parser.add_argument("--rate", type=float, default=6.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--status-interval", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scenes", nargs="+", default=list(SCENES.keys()))
    args = parser.parse_args()

    root = Path(args.output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "total_per_scene": args.total,
        "image_output_format": "jpg",
        "engines": ["baidu", "bing"],
        "clip_filter": False,
        "description": "合规场景图片数据集，用作二分类模型的负样本",
        "scenes": SCENES,
    }
    (root / "crawl_config.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    commands: dict[str, list[str]] = {}
    for name in args.scenes:
        if name not in SCENES:
            print(f"unknown scene: {name}", file=sys.stderr)
            return 1
        scene = SCENES[name]
        output_dir = root / name
        output_dir.mkdir(parents=True, exist_ok=True)
        commands[name] = build_command(scene, output_dir, args.total, args.rate, args.workers)

    if args.dry_run:
        for name, command in commands.items():
            print(name, json.dumps(command, ensure_ascii=False))
        return 0

    pending = deque(commands)
    running: dict[str, tuple[subprocess.Popen[bytes], object]] = {}
    exit_codes: dict[str, int] = {}
    stopping = False

    def stop_children(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        print(f"received signal {signum}; asking child crawlers to stop", flush=True)
        for process, _log_handle in running.values():
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop_children)
    signal.signal(signal.SIGTERM, stop_children)

    last_status = 0.0
    while pending or running:
        now = time.time()
        while pending and len(running) < args.parallel and not stopping:
            name = pending.popleft()
            log_path = root / f"{name}.crawl.log"
            log_handle = open(log_path, "ab", buffering=0)
            print(f"[{time.strftime('%H:%M:%S')}] starting {name} (pid will follow)", flush=True)
            process = subprocess.Popen(
                commands[name],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(root.parent),
            )
            running[name] = (process, log_handle)
            print(f"[{time.strftime('%H:%M:%S')}] {name} pid={process.pid}", flush=True)

        finished: list[str] = []
        for name, (process, log_handle) in running.items():
            if process.poll() is not None:
                finished.append(name)
                exit_codes[name] = process.returncode
                log_handle.close()
                print(
                    f"[{time.strftime('%H:%M:%S')}] {name} finished exit={process.returncode} "
                    f"images={image_count(root / name)}",
                    flush=True,
                )
        for name in finished:
            del running[name]

        if now - last_status >= args.status_interval:
            last_status = now
            parts = [f"pending={len(pending)}", f"running={len(running)}"]
            for name, (process, _) in running.items():
                parts.append(f"{name}={image_count(root / name)}")
            print(f"[{time.strftime('%H:%M:%S')}] status: {' '.join(parts)}", flush=True)

        if running:
            time.sleep(2.0)
        elif pending and not stopping:
            time.sleep(0.5)

    failed = {name: code for name, code in exit_codes.items() if code != 0}
    print(f"\n{'='*60}")
    print(f"crawl summary: total_scenes={len(exit_codes)}, failed={len(failed)}")
    for name, code in sorted(exit_codes.items()):
        print(f"  {name}: exit={code}, images={image_count(root / name)}")
    if failed:
        print(f"failed scenes: {', '.join(sorted(failed))}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
