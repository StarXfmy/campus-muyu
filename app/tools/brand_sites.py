"""品牌官网映射：问品牌时先给官网入口，再给新闻。

数据来自用户整理的清单；只放主站，区域分散/多域名的按备注选主入口。
说明：多数品牌官网没有 /blog，official_posts 抓不到发布列表；
这里保证的是一手入口（官网链接），新闻仍走 RSS + 搜索兜底。
"""
from __future__ import annotations

import re

# (英文/别名小写, 中文名, 官网)
BRANDS = (
    # 数码电子
    ("apple", "苹果", "https://www.apple.com.cn"),
    ("xiaomi", "小米", "https://www.mi.com"),
    ("huawei", "华为", "https://consumer.huawei.com/cn/"),
    ("vmall", "华为商城", "https://www.vmall.com"),
    ("honor", "荣耀", "https://www.hihonor.com/cn/"),
    ("samsung", "三星", "https://www.samsung.com/cn/"),
    ("oppo", "OPPO", "https://www.oppo.com/cn/"),
    ("vivo", "vivo", "https://www.vivo.com.cn"),
    ("oneplus", "一加", "https://www.oneplus.com/cn"),
    ("lenovo", "联想", "https://www.lenovo.com.cn"),
    ("meizu", "魅族", "https://www.meizu.com"),
    ("nubia", "努比亚", "https://www.nubia.com/cn/"),
    # 运动户外
    ("nike", "耐克", "https://www.nike.com.cn"),
    ("adidas", "阿迪达斯", "https://www.adidas.com.cn"),
    ("anta", "安踏", "https://www.anta.com"),
    ("lining", "李宁", "https://www.lining.com"),
    ("puma", "彪马", "https://www.puma.com"),
    ("converse", "匡威", "https://www.converse.com"),
    ("fila", "斐乐", "https://www.fila.com.cn"),
    ("peak", "匹克", "https://www.epeaksport.com"),
    ("erke", "鸿星尔克", "https://www.erke.com"),
    ("asics", "亚瑟士", "https://www.asics.com"),
    ("mizuno", "美津浓", "https://www.mizuno.com"),
    ("decathlon", "迪卡侬", "https://www.decathlon.com.cn"),
    # 零售与超市
    ("walmart", "沃尔玛", "https://www.walmart.cn"),
    ("samsclub", "山姆会员商店", "https://www.samsclub.cn"),
    ("rt-mart", "大润发", "https://www.rt-mart.com.cn"),
    ("freshippo", "盒马鲜生", "https://www.freshippo.com"),
    ("yonghui", "永辉超市", "https://www.yonghui.com.cn"),
    ("lawson", "罗森", "https://www.lawson.com.cn"),
    ("familymart", "全家", "https://www.familymart.com.cn"),
    # 服饰快时尚与家居
    ("uniqlo", "优衣库", "https://www.uniqlo.cn"),
    ("zara", "ZARA", "https://www.zara.cn"),
    ("hm", "H&M", "https://www.hm.com.cn"),
    ("muji", "无印良品", "https://www.muji.com.cn"),
    ("ikea", "宜家", "https://www.ikea.cn"),
    ("miniso", "名创优品", "https://www.miniso.cn"),
    ("watsons", "屈臣氏", "https://www.watsons.com.cn"),
    # 咖啡与茶饮
    ("starbucks", "星巴克", "https://www.starbucks.com.cn"),
    ("luckin", "瑞幸咖啡", "https://www.lkcoffee.com"),
    ("mxbc", "蜜雪冰城", "https://www.mxbc.com"),
    ("heytea", "喜茶", "https://www.heytea.com"),
    ("chagee", "霸王茶姬", "https://www.chagee.com"),
    ("guming", "古茗", "https://www.gumingnc.com"),
    ("chabaidao", "茶百道", "https://www.chabaidao.com"),
    ("hsay", "沪上阿姨", "https://www.hsay.com"),
    ("manner", "Manner Coffee", "https://www.wearemanner.com"),
    ("mstand", "M Stand", "https://www.mstand.cn"),
    ("cotti", "库迪咖啡", "https://www.cotticoffee.com"),
    ("tims", "Tims天好咖啡", "https://www.timschina.com"),
    # 食品饮料
    ("coca-cola", "可口可乐", "https://www.coca-cola.com.cn"),
    ("nongfu", "农夫山泉", "https://www.nongfuspring.com"),
    ("yili", "伊利", "https://www.yili.com"),
    ("mengniu", "蒙牛", "https://www.mengniu.com.cn"),
    ("haagen-dazs", "哈根达斯", "https://www.haagendazs.com.cn"),
    ("nestle", "雀巢", "https://www.nestle.com.cn"),
    # 美妆护肤
    ("loreal", "巴黎欧莱雅", "https://www.lorealparis.com.cn"),
    ("lancome", "兰蔻", "https://www.lancome.com.cn"),
    ("esteelauder", "雅诗兰黛", "https://www.esteelauder.com.cn"),
    ("lamer", "海蓝之谜", "https://www.lamer.com.cn"),
    ("kiehls", "科颜氏", "https://www.kiehls.com.cn"),
    ("winona", "薇诺娜", "https://www.winona.com.cn"),
    ("proya", "珀莱雅", "https://www.polachina.com"),
    ("shiseido", "资生堂", "https://www.shiseido.com.cn"),
    ("clarins", "娇韵诗", "https://www.clarins.com.cn"),
    ("kans", "韩束", "https://www.kans.cn"),
    # 书店与文具
    ("sisyphe", "西西弗书店", "https://www.sisyphe.com.cn"),
    ("xhsdzd", "新华书店", "https://www.xhsd.com"),
    ("deli", "得力", "https://www.nbdeli.com"),
    ("mg-pen", "晨光文具", "https://www.mg-pen.com"),
    # 出行与物流
    ("hello", "哈啰出行", "https://www.helloglobal.com"),
    ("sf-express", "顺丰速运", "https://www.sf-express.com"),
    ("zto", "中通快递", "https://www.zto.com"),
    ("yto", "圆通速递", "https://www.yto.net.cn"),
    ("sto", "申通快递", "https://www.sto.cn"),
    ("yunda", "韵达快递", "https://www.yundaex.com"),
    ("jdwl", "京东物流", "https://www.jdwl.com"),
    ("deppon", "德邦物流", "https://www.deppon.com"),
    ("ems", "中国邮政速递", "https://www.ems.com.cn"),
    # 运营商
    ("10086", "中国移动", "https://www.10086.cn"),
    ("10010", "中国联通", "https://www.10010.com"),
    ("189", "中国电信", "https://www.189.cn"),
    ("10099", "中国广电", "https://www.10099.com.cn"),
    # 视频与娱乐
    ("iqiyi", "爱奇艺", "https://www.iqiyi.com"),
    ("bilibili", "哔哩哔哩", "https://www.bilibili.com"),
    ("youku", "优酷", "https://www.youku.com"),
    # 学术与学习
    ("cnki", "中国知网", "https://www.cnki.net"),
    ("chaoxing", "超星学习通", "https://www.chaoxing.com"),
    # 互联网与生活服务
    ("tencent", "腾讯", "https://www.tencent.com"),
    ("toutiao", "今日头条", "https://www.toutiao.com"),
    ("alibaba", "阿里巴巴", "https://www.alibaba.com"),
    ("baidu", "百度", "https://www.baidu.com"),
    ("netease", "网易", "https://www.163.com"),
    ("meituan", "美团", "https://www.meituan.com"),
    ("taobao", "淘宝", "https://www.taobao.com"),
    ("tmall", "天猫", "https://www.tmall.com"),
    ("alipay", "支付宝", "https://www.alipay.com"),
    ("jd", "京东", "https://www.jd.com"),
    ("dangdang", "当当", "https://www.dangdang.com"),
)


# 有独立新闻/发布页的品牌（实测可达）：品牌 key → 新闻页路径
NEWS_PATHS = {
    "apple": "/newsroom",
    "samsung": "/news",
    "huawei": "/press",
    "honor": "/news",
    "oneplus": "/blog",
    "walmart": "/newsroom",
    "tencent": "/newsroom",
    "ikea": "/newsroom",
    "meituan": "/news",
}


def find_brand(text: str) -> dict | None:
    """在文本里找品牌，长别名优先；英文别名要求词边界，避免 jd 命中 systems。"""
    raw = text or ""
    low = raw.lower()
    best: dict | None = None
    for alias, name, site in BRANDS:
        hit = False
        if alias:
            hit = bool(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", low))
        if not hit and name and name in raw:
            hit = True
        if hit and (best is None or len(alias) > len(best["key"])):
            path = NEWS_PATHS.get(alias)
            best = {
                "key": alias,
                "name": name,
                "site": site,
                "news": (site.rstrip("/") + path) if path else "",
            }
    return best
