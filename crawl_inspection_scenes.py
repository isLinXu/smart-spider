#!/usr/bin/env python3
"""Ten inspection scenes, positive/negative candidate images, resumable batches."""
from __future__ import annotations

import os
import sys
import crawl_safety_scenes as runner

# Positive means the named event is present, not compliance.
DEFINITIONS = [
    ('intrusion', '人员闯入', '禁区人员闯入|人员翻越围栏|人员进入危险区域|铁路行人闯入|工厂禁区有人|person entering restricted area|person climbing security fence|pedestrian in restricted industrial area', '无人禁区|空旷厂区围栏|无人危险区域|封闭设备区域无人|empty restricted area|empty fenced industrial area|clear railway track', '需要指定禁入区域；普通有人画面不能直接标为闯入。'),
    ('absence', '离岗识别', '值班室空岗|值班岗位无人|保安岗亭无人|监控室无人值守|生产岗位无人|empty security booth|unattended control room|empty operator workstation', '保安在岗值守|监控室人员值班|操作工在岗工作|前台人员在岗|security guard on duty|operator at control room workstation|staff at reception desk', '空岗候选需结合岗位区域、排班与离岗持续时间复核。'),
    ('smoking', '抽烟识别', '工人抽烟|员工吸烟|室内人员抽烟|手持香烟吸烟|监控吸烟抓拍|worker smoking cigarette|person holding lit cigarette|person smoking indoors', '工人正常作业|员工喝水|人员吃东西|员工手摸脸|工人正常交流|worker drinking water|person eating food|worker touching face', '负样本需确认无香烟及吸烟行为，保留喝水、手靠近嘴等难负例。'),
    ('no_chef_hat', '未戴厨师帽识别', '厨师未戴帽子|后厨人员未戴厨师帽|餐饮员工未戴工作帽|厨房工作人员头发外露|chef cooking without hat|kitchen worker without hairnet|cook with uncovered hair', '厨师戴厨师帽|后厨人员戴工作帽|厨房员工戴发网|食品加工人员戴帽|chef wearing chef hat|kitchen worker wearing hairnet|cook wearing kitchen cap', '正负样本都需要可见厨房工作人员头部，帽子或发网合规标准需复核。'),
    ('no_mask', '未戴口罩识别', '后厨人员未戴口罩|食品加工员工未戴口罩|厨师露出口鼻|厨房工人口罩戴下巴|kitchen worker without face mask|chef without mask|food worker mask below chin', '后厨人员正确戴口罩|食品加工员工戴口罩|厨师口罩遮住口鼻|chef wearing face mask|food worker wearing surgical mask|kitchen staff masked', '口鼻必须清晰可见；遮挡或无法辨认的样本进入复核。'),
    ('no_uniform', '未穿工作服识别', '后厨人员穿便服|食品加工人员未穿工作服|厨师未穿厨师服|工厂员工穿便装|kitchen worker in casual clothes|food worker without uniform|factory worker casual clothing', '厨师穿厨师服|厨房员工穿工作服|食品加工人员穿工作服|工厂工人穿工服|chef wearing chef uniform|food processing worker uniform|factory worker in workwear', '合规服装依岗位而定，需按部署场景确认。'),
    ('open_flame', '明火识别', '室内明火|仓库火焰|工厂火灾明火|厨房油锅起火|垃圾桶明火|indoor open flame|warehouse fire flames|kitchen pan fire|industrial fire flames', '无火焰厨房|正常仓库内部|工厂车间正常生产|红色灯光室内|夕阳照射仓库|warehouse interior|industrial red warning light|sunlight in factory', '正样本为可见真实火焰；反光、灯光、红色物体作为难负例。'),
    ('smoke', '烟雾识别', '火灾烟雾|厂房浓烟|室内烟雾|仓库冒烟|厨房火灾浓烟|fire smoke indoors|warehouse smoke|industrial fire smoke|kitchen fire smoke', '厨房水蒸气|蒸锅蒸汽|工厂水蒸气|室外雾气|扬尘现场|kitchen steam|steam from boiling water|fog industrial area|dust cloud construction', '烟雾与蒸汽、雾、扬尘外观相近，需要复核来源。'),
    ('rat', '老鼠识别', '厨房老鼠|仓库老鼠|餐厅老鼠|地面老鼠|下水道老鼠|rat on kitchen floor|mouse in warehouse|rat in restaurant|rodent on floor', '干净厨房地面|仓库地面杂物|厨房地面电线|地面小鸟|地面猫|empty kitchen floor|warehouse floor debris|cable on floor|small bird on ground', '正样本需要可见鼠类；负样本确认无鼠，保留线缆与小动物干扰。'),
    ('camera_abnormal', '摄像头异常识别', '监控画面遮挡|监控画面模糊|监控画面黑屏|摄像头雪花屏|监控画面过曝|监控画面偏色|blocked CCTV view|blurry CCTV footage|CCTV black screen|CCTV video noise|overexposed security camera', '清晰监控画面|正常监控夜视画面|正常仓库监控|正常厨房监控|clear CCTV footage|normal night vision security camera|clear warehouse surveillance', '采集实际异常画面而非摄像头产品照片；低照度不自动等于异常。'),
]


def make_scenes():
    scenes = {}
    for key, label, positive, negative, rule in DEFINITIONS:
        for polarity, phrases in [('positive', positive), ('negative', negative)]:
            seeds = phrases.split('|')
            keywords = list(seeds)
            for phrase in seeds:
                suffixes = [' 现场照片', ' 监控画面', ' 实拍'] if any('\u4e00' <= c <= '\u9fff' for c in phrase) else [' photo', ' CCTV footage', ' real scene']
                keywords.extend(phrase + suffix for suffix in suffixes)
            scenes[f'{key}/{polarity}'] = {
                'label': f'{label}_{"正样本" if polarity == "positive" else "负样本"}',
                'keywords': keywords,
                'review_required': True,
                'review_rule': rule,
                'sample_status': 'unverified_keyword_candidate',
            }
    return scenes


if __name__ == '__main__':
    # Direct public connections avoid implicit macOS loopback proxy selection.
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    runner.SCENES = make_scenes()
    # Existing driver handles process limits, independent logs and resume state.
    sys.argv[1:1] = ['--output-root', 'dataset_inspection_scenes_20260915',
                     '--total', '2000', '--parallel', '2', '--workers', '6', '--rate', '2']
    raise SystemExit(runner.main())
