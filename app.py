import streamlit as st
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import pandas as pd
import time

# 设置页面标题
st.set_page_config(page_title="酒店房价监控系统", layout="wide")

st.title("🏨 1 Hotels 实时房价爬虫控制台")
st.write("直接通过部署在 Streamlit Cloud 上的无头浏览器模拟访问并解析数据。")

# 默认填入你提供的 1 Hotels 预订链接
default_url = "https://www.1hotels.com/book/60507?startDate=2026-07-07&endDate=2026-07-08&adults=1&children=0&exactMatchOnly=false&language=en&dogs=false&cats=false&rooms=%5B%5D&currency=USD&groupCode=&promoCode=&sort=low"
target_url = st.text_area("请输入要爬取的酒店 URL:", value=default_url, height=100)

# 初始化 Selenium WebDriver 的函数（兼容 Streamlit Linux 环境）
def init_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")  # 无界面模式
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    # 伪装 User-Agent 防止被阻挡
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # 在 Streamlit Cloud 上，Chromium 通常安装在标准路径，Selenium 会自动寻找
    driver = webdriver.Chrome(options=chrome_options)
    return driver

# 点击按钮开始爬取
if st.button("🚀 开始抓取实时房价", type="primary"):
    if not target_url.strip():
        st.error("请输入有效的 URL 网址！")
    else:
        with st.spinner("正在启动云端无头浏览器，模拟用户进入官网... (这可能需要 20-40 秒)"):
            driver = None
            try:
                driver = init_driver()
                # 访问目标网页
                driver.get(target_url)
                
                st.info("网页已打开，正在等待酒店房型和价格组件加载...")
                
                # 【核心攻坚：等待网页加载】
                # 这里使用显式等待，等待页面上包含房型信息的任意元素加载完成
                # 注意：如果官网改版，这里的 "body" 可以改成具体的类名如 "div.room-card"
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                
                # 额外强制等待 5 秒，确保异步 API 的价格数据完全渲染出来
                time.sleep(5)
                
                # 获取加载完 JS 后的 HTML 源码
                html_source = driver.page_source
                
                # 使用 BeautifulSoup 解析
                soup = BeautifulSoup(html_source, "html.parser")
                
                # -------------------------------------------------------------
                # 数据解析核心块（这里以通用的“卡片-名称-价格”逻辑演示，需根据官网结构做精准匹配）
                # -------------------------------------------------------------
                rooms_list = []
                
                # ⚠️ 1hotels 预订系统通常使用一些特定的类名，这里需要通过浏览器 F12 抓取后动态调整
                # 假设房型区块包含在名为 'room-rate-card' 的 div 中
                room_cards = soup.find_all("div", class_=["room-card", "rate-card", "room-type"]) 
                
                # 如果通用抓取没找到，尝试在整个页面寻找包含价格符号 $ 的文本区块
                if not room_cards:
                    st.warning("未检测到标准的房型组件类名，改用深度文本嗅探解析...")
                    # 尝试寻找页面所有的 H2, H3 标签作为房型名字，并在其附近寻找价格
                    for container in soup.find_all(["div", "section"]):
                        h_tag = container.find(["h2", "h3", "h4"])
                        if h_tag and "$" in container.get_text():
                            text_content = container.get_text(separator="|").strip()
                            rooms_list.append({"原始抓取数据块": text_content})
                else:
                    for card in room_cards:
                        # 提取名字和价格
                        name = card.find(["h2", "h3", "span"])
                        price = card.find(text=lambda text: text and "$" in text)
                        
                        name_text = name.get_text().strip() if name else "未知房型"
                        price_text = price.strip() if price else "查看官网"
                        
                        rooms_list.append({"房型名称": name_text, "实时房价": price_text})

                # -------------------------------------------------------------
                # 结果展示
                # -------------------------------------------------------------
                if rooms_list:
                    df = pd.DataFrame(rooms_list)
                    st.success("🎉 数据抓取并解析成功！")
                    
                    # 在 Streamlit 上展示漂亮的表格
                    st.subheader("📊 实时房型价格明细表")
                    st.dataframe(df, use_container_width=True)
                    
                    # 提供 CSV 下载功能
                    csv_data = df.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        label="📥 下载数据为 CSV 文件",
                        data=csv_data,
                        file_name="1hotels_prices.csv",
                        mime="text/csv"
                    )
                    
                    # 打印部分的原始 HTML 快照方便调试
                    with st.expander("🔍 调试查看：页面原始文本骨架"):
                        st.text(soup.get_text()[:2000]) # 打印前 2000 个字
                else:
                    st.error("未能从小程序/官网页面中解析出价格。可能触发了 Cloudflare 防爬验证，或官网类名已变更。")
                    with st.expander("查看反爬返回的页面状态"):
                        st.text(soup.get_text()[:1000])
                        
            except Exception as e:
                st.error(f"❌ 运行中发生错误: {str(e)}")
            finally:
                if driver:
                    driver.quit()
