#!/usr/bin/env python3
"""Crawl six workplace-safety image candidate datasets in parallel.

Each scene has its own output directory, fixed Chinese label, crawl log, URL
deduplication state, and resumable dataset state.  Search phrases intentionally
mix Chinese and English to broaden source coverage.
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
    "phone_call": {
        "label": "打电话",
        "keywords": [
            "打电话", "接打电话", "手机打电话", "正在打电话的人", "工作时打电话",
            "员工工作时打电话", "工人上班打电话", "工人打电话", "工厂工人打电话",
            "车间工人打电话", "仓库员工打电话", "物流员工打电话", "施工现场打电话",
            "建筑工人打电话", "厂区人员接电话", "操作岗位打电话", "工作场所电话通话",
            "安全监控人员打电话", "违规使用电话抓拍", "上班接听手机",
            "worker talking on phone", "employee making a phone call",
            "factory worker talking on cellphone", "warehouse worker phone call",
            "construction worker talking on phone", "industrial worker phone call",
            "worker answering mobile phone", "workplace phone call CCTV",
            "operator talking on cellphone at work", "employee calling at work",
            "工厂员工接电话", "仓库工人接电话", "物流工人接电话", "施工人员接电话",
            "车间人员打手机", "岗位员工接听电话", "生产现场接电话", "厂区接打手机",
            "员工电话通话安全违章", "工作时间电话通话抓拍",
            # 去重后补采备用词：保持与原查询不同，避免断点状态跳过。
            "工厂现场人员手机通话", "仓库作业人员接电话", "物流装卸工接听手机",
            "施工现场工人电话通话", "生产岗位员工打电话照片", "工地作业打电话行为",
            "industrial workplace worker on phone", "warehouse employee phone conversation",
            "factory operator making a call", "construction site worker on mobile phone",
            "worker using telephone during shift", "employee phone call safety violation",
            "工厂监控打电话行为", "车间安全监控接电话", "仓库监控人员打手机",
            "作业区域接打电话隐患", "上岗期间手机通话", "员工接听电话现场照片",
            "manufacturing worker answering phone", "workplace worker cellphone conversation",
        ],
    },
    "mobile_use": {
        "label": "使用手机",
        "keywords": [
            "使用手机", "正在玩手机", "低头看手机", "工作时使用手机", "上班玩手机",
            "员工上班看手机", "工人工作时看手机", "工人玩手机", "工厂员工使用手机",
            "车间工人看手机", "仓库员工玩手机", "物流人员使用手机", "施工现场玩手机",
            "建筑工人看手机", "厂区违规使用手机", "操作岗位玩手机", "边工作边看手机",
            "监控抓拍员工玩手机", "工作场所刷手机", "工人发短信",
            "worker using smartphone", "employee looking at mobile phone",
            "factory worker using cellphone", "warehouse worker using phone",
            "construction worker looking at smartphone", "worker texting at work",
            "industrial worker distracted by phone", "employee browsing phone at work",
            "workplace cellphone use CCTV", "operator using mobile phone",
            "工厂员工玩手机", "仓库工人看手机", "物流工人玩手机", "施工人员看手机",
            "车间人员刷手机", "岗位员工使用手机", "生产现场看手机", "厂区玩手机违章",
            "员工工作时间看手机抓拍", "工作场所手机安全违章",
            "工厂现场人员看手机", "仓库作业人员刷手机", "物流装卸工使用手机",
            "施工现场工人低头看手机", "生产岗位员工玩手机照片", "工地作业看手机行为",
            "industrial workplace worker using smartphone", "warehouse employee checking phone",
            "factory operator distracted by cellphone", "construction site worker using mobile",
            "worker browsing phone during shift", "employee smartphone safety violation",
            "工厂监控玩手机行为", "车间安全监控看手机", "仓库监控人员使用手机",
            "作业区域手机分心隐患", "上岗期间刷手机", "员工查看手机现场照片",
            "manufacturing worker checking mobile", "workplace worker looking at cellphone",
        ],
    },
    "smoking": {
        "label": "吸烟",
        "keywords": [
            "吸烟", "抽烟的人", "正在抽烟", "工作时吸烟", "上班抽烟", "员工工作场所吸烟",
            "工人吸烟", "工厂工人抽烟", "车间员工吸烟", "仓库人员抽烟", "物流园工人吸烟",
            "施工现场吸烟", "建筑工人抽烟", "厂区违规吸烟", "禁烟区吸烟", "岗位上抽烟",
            "监控抓拍吸烟", "安全检查发现吸烟", "室内工作场所抽烟", "工地违规抽烟",
            "worker smoking", "employee smoking at work", "factory worker smoking",
            "warehouse worker smoking", "construction worker smoking cigarette",
            "industrial worker smoking", "smoking in no smoking area",
            "workplace smoking CCTV", "worker holding lit cigarette", "on duty smoking violation",
            "工厂员工抽烟", "仓库工人吸烟", "物流工人抽烟", "施工人员吸烟",
            "车间人员抽烟", "岗位员工吸烟违章", "生产现场抽烟", "厂区抽烟抓拍",
            "员工禁烟区抽烟", "工作时间吸烟安全违章",
            "工厂现场人员点烟", "仓库作业人员抽烟", "物流装卸工吸烟",
            "施工现场工人点火抽烟", "生产岗位员工吸烟照片", "工地作业吸烟行为",
            "industrial workplace worker with cigarette", "warehouse employee smoking break",
            "factory operator smoking cigarette", "construction site worker smoking photo",
            "worker smoking during shift", "employee cigarette safety violation",
            "工厂监控吸烟行为", "车间安全监控抽烟", "仓库监控人员吸烟",
            "作业区域吸烟隐患", "上岗期间抽烟", "员工吸烟现场照片",
            "manufacturing worker holding cigarette", "workplace worker smoking violation",
        ],
    },
    "no_reflective_vest": {
        "label": "未穿反光衣",
        "keywords": [
            "未穿反光衣", "没穿反光衣", "未穿反光背心", "未穿警示背心", "工人未穿反光衣",
            "员工没穿反光背心", "作业人员未穿反光衣", "施工人员未穿反光背心",
            "建筑工人未穿反光衣", "道路工人没穿反光背心", "仓库工人未穿反光衣",
            "物流园人员未穿反光衣", "厂区人员未穿反光背心", "装卸工未穿反光衣",
            "未穿反光衣违章", "未穿反光衣监控抓拍", "未穿反光衣安全隐患",
            "未穿高可视背心", "无反光衣作业", "反光衣穿戴不规范",
            "worker without reflective vest", "worker without safety vest",
            "construction worker no reflective vest", "warehouse worker without hi vis vest",
            "road worker no high visibility vest", "industrial worker without hi vis clothing",
            "employee not wearing reflective vest", "loading worker without safety vest",
            "no safety vest workplace violation", "worker missing high visibility vest CCTV",
            "工厂员工未穿反光衣", "仓库工人没穿反光背心", "物流工人未穿反光衣",
            "施工人员未穿高可视背心", "车间人员未穿警示背心", "作业人员反光衣违章",
            "生产现场未穿反光衣抓拍", "厂区安全检查未穿反光衣", "装卸作业无反光衣",
            "工作场所未穿反光衣隐患",
            "工厂现场工人未穿高可视衣", "仓库作业人员无反光背心", "物流装卸工未穿警示衣",
            "施工现场人员缺少反光衣", "生产岗位无高可视背心照片", "工地作业未穿安全背心",
            "industrial worker without high visibility clothing", "warehouse worker no safety vest photo",
            "factory employee missing reflective jacket", "construction worker lacking hi vis vest",
            "worker PPE violation no reflective vest", "employee without visibility vest at work",
            "工厂监控未穿反光衣", "车间安全监控无反光背心", "仓库监控人员未穿警示服",
            "作业区域反光衣缺失隐患", "上岗期间未穿高可视服", "员工未穿背心现场照片",
            "manufacturing worker missing safety vest", "workplace worker no high visibility jacket",
        ],
    },
    "forklift_no_helmet": {
        "label": "叉车司机未戴安全帽",
        "engines": "baidu",
        "keywords": [
            "叉车司机未戴安全帽", "叉车司机没戴安全帽", "叉车工未戴安全帽",
            "叉车驾驶员未戴安全帽", "叉车操作员没戴安全帽", "叉车司机不戴安全帽",
            "开叉车未戴安全帽", "驾驶叉车没戴安全帽", "叉车作业未戴头盔",
            "叉车司机未佩戴头盔", "叉车工安全帽违章", "叉车司机安全违章",
            "叉车司机未戴安全帽抓拍", "叉车司机未戴安全帽监控", "厂区叉车司机没戴安全帽",
            "仓库叉车司机未戴安全帽", "物流叉车司机未戴安全帽", "车间叉车工未戴安全帽",
            "无安全帽叉车作业", "叉车驾驶员劳保穿戴不规范",
            "forklift driver without hard hat", "forklift operator no helmet",
            "forklift driver not wearing safety helmet", "forklift operator without hardhat",
            "warehouse forklift driver no hard hat", "factory forklift operator without helmet",
            "forklift PPE violation", "forklift driver missing safety helmet",
            "forklift operator no hard hat CCTV", "unsafe forklift driver without helmet",
            # 备用扩展词：否定语义检索结果不足时，用同场景的宽词补足候选，
            # 后续可结合人工复核/视觉模型清洗。
            "叉车司机", "叉车驾驶员", "叉车操作员", "叉车工人", "叉车作业现场",
            "仓库叉车作业", "工厂叉车作业", "物流叉车作业", "叉车安全帽",
            "叉车 PPE", "叉车违章作业", "叉车司机安全检查", "叉车作业安全隐患",
            "叉车司机劳保用品", "叉车人员防护用品", "叉车驾驶员头部防护",
            "forklift driver warehouse", "forklift operator factory", "forklift workplace safety",
            "forklift safety inspection", "forklift PPE safety", "forklift operator at work",
            "warehouse forklift operation", "factory forklift operation", "industrial forklift driver",
            "forklift driver protective equipment", "forklift safety violation", "forklift hazard",
            "forklift operator workplace CCTV", "forklift driver safety audit", "forklift loading warehouse",
            "叉车师傅", "叉车驾驶作业", "叉车装卸现场", "仓储叉车司机", "物流园叉车司机",
            "生产车间叉车司机", "厂内叉车驾驶员", "货运站叉车操作员", "叉车司机工作照",
            "叉车驾驶现场实拍", "叉车作业监控", "叉车司机监控画面", "叉车装货作业",
            "叉车卸货作业", "叉车搬运货物", "室内叉车司机", "室外叉车司机",
            "电动叉车司机", "燃油叉车司机", "坐式叉车司机", "叉车安全作业",
            "forklift driver working", "forklift operator warehouse photo", "forklift driver factory photo",
            "forklift loading operation", "forklift unloading operation", "forklift operator CCTV footage",
            "industrial forklift operation photo", "forklift driver workplace photo", "forklift operator safety photo",
            "叉车司机现场作业未戴头盔", "叉车驾驶员仓库作业照片", "叉车操作员工厂现场",
            "叉车司机装卸货物实拍", "叉车司机头部未防护", "叉车作业人员无安全帽",
            "forklift driver operating without helmet photo", "forklift operator warehouse worker",
            "forklift driver loading goods no hardhat", "forklift workplace helmet violation",
            "forklift operator head protection missing", "forklift driver industrial safety photo",
            "厂内叉车作业人员未戴头盔", "仓储叉车驾驶员安全帽缺失",
            "物流园叉车司机防护违规", "车间叉车司机头部防护不足",
            "forklift driver safety PPE missing", "forklift operator no protective helmet",
        ],
    },
    "items_left": {
        "label": "物品滞留",
        "keywords": [
            "物品滞留", "物料滞留", "货物滞留", "通道物品滞留", "仓库物品滞留",
            "车间物料滞留", "厂区杂物滞留", "作业区物品堆放", "通道堆放物品",
            "仓库通道堆货", "车间通道物料堆积", "物流通道货物堆积", "走道杂物堆放",
            "安全出口物品堆放", "消防通道堆放杂物", "疏散通道货物占用", "设备旁物品滞留",
            "生产线物料积压", "工作场所杂物堆积", "现场物品未清理",
            "items left in workplace", "materials left in walkway", "goods blocking warehouse aisle",
            "objects obstructing factory passage", "cluttered workplace aisle",
            "boxes blocking emergency exit", "warehouse aisle obstruction",
            "materials accumulation in factory", "workplace trip hazard objects",
            "unattended goods in work area",
            "工厂通道物品堆放", "仓库走道货物滞留", "物流中心通道堵塞", "车间杂物占道",
            "生产现场物料未清理", "厂区安全通道堆物", "仓储区域物品堆积", "装卸区货物滞留",
            "工作区域物品阻塞通道", "消防通道物品滞留",
            "工厂现场通道杂物遗留", "仓库作业区货物未移走", "物流装卸区物品滞留",
            "施工现场材料占用通道", "生产岗位物料堆积照片", "工地安全通道堆放物",
            "industrial workplace cluttered passage", "warehouse goods left in aisle photo",
            "factory materials blocking walkway", "construction site objects left behind",
            "workplace aisle obstruction safety violation", "employee area unattended materials",
            "工厂监控通道堆物", "车间安全监控物品滞留", "仓库监控货物占道",
            "作业区域杂物阻塞隐患", "安全出口前物品遗留", "员工工作区物品未清理",
            "manufacturing workplace objects obstructing aisle", "warehouse clutter safety hazard",
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
    parser.add_argument("--output-root", default="dataset_safety_scenes_20000_20260902")
    parser.add_argument("--total", type=int, default=20_000)
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--rate", type=float, default=6.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--status-interval", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "total_per_scene": args.total,
        "image_output_format": "jpg",
        "engines": ["baidu", "bing"],
        "clip_filter": False,
        "scenes": SCENES,
    }
    (root / "crawl_config.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    commands: dict[str, list[str]] = {}
    for name, scene in SCENES.items():
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

    try:
        while pending or running:
            while pending and len(running) < max(1, args.parallel) and not stopping:
                name = pending.popleft()
                output_dir = root / name
                log_handle = (output_dir / "crawl.log").open("ab", buffering=0)
                process = subprocess.Popen(
                    commands[name],
                    cwd=Path(__file__).resolve().parent,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
                running[name] = (process, log_handle)
                print(f"started {name}: pid={process.pid}", flush=True)

            finished = []
            for name, (process, log_handle) in running.items():
                code = process.poll()
                if code is not None:
                    exit_codes[name] = code
                    log_handle.close()
                    finished.append(name)
                    print(
                        f"finished {name}: exit={code} images={image_count(root / name)}",
                        flush=True,
                    )
            for name in finished:
                del running[name]

            counts = ", ".join(
                f"{name}={image_count(root / name)}/{args.total}" for name in SCENES
            )
            print(f"status: {counts}", flush=True)
            if running:
                time.sleep(max(1.0, args.status_interval))
            elif pending and not stopping:
                continue
            elif stopping:
                break
    finally:
        for process, log_handle in running.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
            log_handle.close()

    summary = {
        name: {
            "label": scene["label"],
            "images": image_count(root / name),
            "target": args.total,
            "exit_code": exit_codes.get(name),
        }
        for name, scene in SCENES.items()
    }
    (root / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if all(item["images"] >= args.total for item in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
