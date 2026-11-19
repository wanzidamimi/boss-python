import requests
import time
import random
import http.cookiejar
from urllib.parse import urljoin, urlencode, quote, urlparse
import json
from bs4 import BeautifulSoup
import pandas as pd
import re

# --- 配置 ---
COOKIE_FILE = 'cookies.txt'
SEARCH_KEYWORDS_LIST = ['']
TARGET_CITIES = {
    "成都": "101270100"
}
EXPERIENCE_CODE = '108'  #
MAX_PAGES_TO_SCRAPE_PER_CITY_KEYWORD = 10  # 先只爬第一页测试
BASE_URL = 'https://www.zhipin.com'
DETAIL_PAGE_DELAY = 2
LIST_PAGE_DELAY_MIN = 2
LIST_PAGE_DELAY_MAX = 3
INTER_KEYWORD_DELAY_MIN = 2
INTER_KEYWORD_DELAY_MAX = 3

# API 端点
SET_TOKEN_URL = f"{BASE_URL}/wapi/zppassport/set/zpToken"
JOB_LIST_API_URL = f"{BASE_URL}/wapi/zpgeek/search/joblist.json"
JOB_DETAIL_API_URL = f"{BASE_URL}/wapi/zpgeek/job/detail.json"
MAIN_SEARCH_PAGE_URL = f"{BASE_URL}/web/geek/jobs"

# MYSTERIOUS_TOKEN = "IGJnYvfbmv2qeMfn" # 暂时移除

# 请求头
BASE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Connection': 'keep-alive',
    'sec-ch-ua': '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'Sec-Fetch-Site': 'same-origin',
}

# 定义需要更新Cookie的错误码
COOKIE_REFRESH_ERROR_CODES = [17, 37]
MAX_COOKIE_UPDATE_RETRIES_PER_CALL = 1  # 每个API调用点允许的最大Cookie更新重试次数


def load_cookies_from_file(session, cookie_file_path):
    print(f"ℹ️ 尝试从 '{cookie_file_path}' 加载Cookies...")
    cj = http.cookiejar.MozillaCookieJar(cookie_file_path)
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
        # 清空旧的 session cookies, 再加载新的
        session.cookies.clear()
        session.cookies.update(cj)
        print(f"🍪 成功加载/更新了 '{cookie_file_path}' 中的Cookies。")
        return True
    except FileNotFoundError:
        print(f"❌ 错误：Cookie文件 '{cookie_file_path}' 未找到。")
        return False
    except Exception as e:
        print(f"❌ 错误：从 '{cookie_file_path}' 加载Cookie失败: {e}")
        return False


def prompt_for_cookie_update(session):
    print("\n" + "=" * 30)
    print("‼️ 检测到可能需要更新Cookie！‼️")
    print(f"请执行以下操作：")
    print(f"  1. 在您的浏览器中，访问 https://www.zhipin.com 并进行一次成功的搜索。")
    print(f"  2. 从浏览器中导出最新的Cookie到名为 '{COOKIE_FILE}' 的文本文件中，并确保它与此脚本在同一目录。")
    print(f"  3. 覆盖旧的 '{COOKIE_FILE}' 文件。")
    print("=" * 30)

    while True:
        user_input = input(f"完成上述操作后，请输入 'y' 或 'yes' 以继续，或输入 'n' 或 'no' 退出脚本: ").strip().lower()
        if user_input in ['y', 'yes']:
            if load_cookies_from_file(session, COOKIE_FILE):
                print("✅ Cookie已重新加载，尝试继续执行...")
                # 可选：重新调用setToken API（如果它有助于激活新Cookie）
                # call_set_token_api(session)
                return True
            else:
                print("❌ Cookie重新加载失败，请检查文件或再次尝试。")
                # 如果加载失败，可以选择再次提示或退出
                # For simplicity, we'll let the loop ask again or user can choose to exit
        elif user_input in ['n', 'no']:
            print("🛑 用户选择退出脚本。")
            return False
        else:
            print("⚠️ 无效输入，请输入 'y'/'yes' 或 'n'/'no'。")


def get_bst_token(session):
    for cookie in session.cookies:
        if cookie.name == 'bst' and cookie.domain == '.zhipin.com':
            return cookie.value
    return None


def fetch_job_list_page_html(session, page_num, city_code, query, experience_code_param,
                             current_lid=None, current_security_id=None,
                             retries_left=MAX_COOKIE_UPDATE_RETRIES_PER_CALL):
    bst_token_value = get_bst_token(session)
    params = {'scene': 1, 'query': query, 'city': city_code, 'page': page_num, 'pageSize': 30,
              '_': int(time.time() * 1000)}
    if experience_code_param: params['experience'] = experience_code_param
    if current_lid: params['lid'] = current_lid
    if current_security_id: params['securityId'] = current_security_id

    headers = BASE_HEADERS.copy()
    referer_params = {'query': query, 'city': city_code}
    if experience_code_param: referer_params['experience'] = experience_code_param
    referer_url = f"{MAIN_SEARCH_PAGE_URL}?{urlencode(referer_params)}"
    headers.update({
        'Accept': 'application/json, text/plain, */*', 'Referer': referer_url,
        'X-Requested-With': 'XMLHttpRequest', 'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors',
    })
    if bst_token_value: headers['zp_token'] = bst_token_value

    try:
        print(f"📄 正在获取 '{query}' 职位列表第 {page_num} 页 (城市: {city_code})")
        response = session.get(JOB_LIST_API_URL, headers=headers, params=params, timeout=30)
        response.raise_for_status()  # 对于非2xx状态码会抛出HTTPError

        response_json = response.json()
        api_code = response_json.get("code")

        if api_code in COOKIE_REFRESH_ERROR_CODES and retries_left > 0:
            print(f"  列表API返回错误码 {api_code} (消息: {response_json.get('message')})，可能需要更新Cookie。")
            if prompt_for_cookie_update(session):
                # Cookie更新成功，重试一次当前请求
                print(f"  重试获取列表API (剩余重试次数: {retries_left - 1})")
                return fetch_job_list_page_html(session, page_num, city_code, query, experience_code_param,
                                                current_lid, current_security_id, retries_left - 1)
            else:  # 用户选择不更新或更新失败
                print("  用户未更新Cookie或更新失败，列表API请求失败。")
                return response_json  # 返回包含错误码的json，让上层处理
        return response_json  # 返回正常的或最终的错误json

    except requests.exceptions.HTTPError as e_http:
        print(f"❌ HTTP错误 - 请求列表API ({query}, 页 {page_num}): {e_http}")
        if e_http.response is not None:
            print(f"  响应状态: {e_http.response.status_code}, 响应内容: {e_http.response.text[:300]}")
            # 某些HTTP错误也可能意味着Cookie问题，但这里主要通过API的code判断
            try:
                error_json = e_http.response.json()
                if error_json.get("code") in COOKIE_REFRESH_ERROR_CODES and retries_left > 0:
                    print(f"  HTTP错误中检测到API错误码 {error_json.get('code')}，尝试Cookie更新流程。")
                    if prompt_for_cookie_update(session):
                        return fetch_job_list_page_html(session, page_num, city_code, query, experience_code_param,
                                                        current_lid, current_security_id, retries_left - 1)
                    else:
                        return None  # 返回None表示请求彻底失败
            except ValueError:  # Not a JSON response
                pass
    except Exception as e:
        print(f"❌ 请求列表API时发生其他错误 ({query}, 页 {page_num}): {e}");
    return None  # 表示请求失败


def fetch_job_detail_api(session, security_id_param, lid_param, current_query, current_city_code,
                         retries_left=MAX_COOKIE_UPDATE_RETRIES_PER_CALL):
    if not security_id_param or not lid_param: return "参数无效 (security_id 或 lid_param 缺失)"

    api_call_delay = random.uniform(DETAIL_PAGE_DELAY, DETAIL_PAGE_DELAY + 5)  # 稍微缩短随机范围
    print(f"    API ⏳ 准备获取详情，延时 {api_call_delay:.2f} 秒...")
    time.sleep(api_call_delay)

    bst_token_value = get_bst_token(session)
    params = {'securityId': security_id_param, 'lid': lid_param, '_': int(time.time() * 1000)}
    headers = BASE_HEADERS.copy()
    referer_params_detail = {'query': current_query, 'city': current_city_code}
    if EXPERIENCE_CODE: referer_params_detail['experience'] = EXPERIENCE_CODE
    referer_url_detail = f"{MAIN_SEARCH_PAGE_URL}?{urlencode(referer_params_detail)}"
    headers.update({
        'Accept': 'application/json, text/plain, */*', 'Referer': referer_url_detail,
        'X-Requested-With': 'XMLHttpRequest', 'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors',
    })
    if bst_token_value: headers['zp_token'] = bst_token_value
    headers.pop('Sec-Fetch-User', None);
    headers.pop('Upgrade-Insecure-Requests', None)

    try:
        print(f"    API 🔍 正在请求职位详情 (lid: {lid_param})")
        response = session.get(JOB_DETAIL_API_URL, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        detail_json = response.json()
        api_code = detail_json.get("code")

        if api_code in COOKIE_REFRESH_ERROR_CODES and retries_left > 0:
            print(f"  详情API返回错误码 {api_code} (消息: {detail_json.get('message')})，可能需要更新Cookie。")
            if prompt_for_cookie_update(session):
                print(f"  重试获取详情API (剩余重试次数: {retries_left - 1})")
                # 注意：当返回字符串时，表示错误信息，而不是JSON对象
                return fetch_job_detail_api(session, security_id_param, lid_param, current_query, current_city_code,
                                            retries_left - 1)
            else:
                return f"获取描述失败 (用户未更新Cookie, API code {api_code})"

        if api_code == 0:
            zp_data = detail_json.get("zpData", {});
            job_info = zp_data.get("jobInfo", {})
            description_from_api = job_info.get("postDescription")
            if description_from_api:
                soup_desc = BeautifulSoup(description_from_api, 'lxml')
                plain_description = soup_desc.get_text(separator='\n', strip=True)
                print(f"    API ✅ 详情提取成功")
                return plain_description
            else:
                print(f"    API ⚠️ JSON中无描述 (zpData.jobInfo.postDescription)"); return "获取描述失败 (JSON中无描述)"
        else:  # 其他非0且非Cookie刷新码的错误
            print(f"    API ❌ 详情API返回错误: code {api_code}, msg: {detail_json.get('message')}")
            return f"获取描述失败 (API code {api_code})"

    except requests.exceptions.HTTPError as e_http_detail:
        print(f"    API ❌ HTTP错误 - 请求详情API (lid: {lid_param}): {e_http_detail}")
        if e_http_detail.response is not None:
            print(
                f"      响应状态: {e_http_detail.response.status_code}, 响应内容: {e_http_detail.response.text[:300]}")
            try:
                error_json = e_http_detail.response.json()
                if error_json.get("code") in COOKIE_REFRESH_ERROR_CODES and retries_left > 0:
                    print(f"  HTTP错误中检测到API错误码 {error_json.get('code')}，尝试Cookie更新流程。")
                    if prompt_for_cookie_update(session):
                        return fetch_job_detail_api(session, security_id_param, lid_param, current_query,
                                                    current_city_code, retries_left - 1)
                    else:
                        return "获取描述失败 (用户未更新Cookie)"
            except ValueError:
                pass  # Not JSON
    except Exception as e:
        print(f"    API ❌ 请求详情API时发生其他错误 (lid: {lid_param}): {e}");
    return f"获取描述失败 (请求异常: {type(e).__name__})"


def parse_job_data_from_api_html(session, job_api_json_response, current_page_num, current_city_name, current_city_code,
                                 current_query):
    extracted_jobs = []
    if not job_api_json_response:
        print(f"⚠️ 传入的API JSON响应为空 (城市 {current_city_name}, 关键词 '{current_query}', 页 {current_page_num})")
        return extracted_jobs, False, None, None  # 添加返回False表示没有更多
    # API code 检查已移至 fetch 函数内部，这里假设 job_api_json_response 是一个有效的json（可能是错误码json）
    if job_api_json_response.get("code") != 0:
        # 如果 fetch 函数处理了 cookie 更新并且仍然失败，它会返回原始的错误 json
        # 我们在这里不需要再次提示 cookie 更新，只需记录错误
        print(
            f"解析API: 列表API返回错误码 (城市 {current_city_name}, 关键词 '{current_query}', 页 {current_page_num}) - Code: {job_api_json_response.get('code')}, Message: {job_api_json_response.get('message')}")
        if job_api_json_response.get("code") in COOKIE_REFRESH_ERROR_CODES:
            print(f"  zpData (验证码相关?): {job_api_json_response.get('zpData')}")
        return extracted_jobs, False, None, None

    zp_data = job_api_json_response.get("zpData", {})
    if not zp_data: print(
        f"⚠️ API响应中无 'zpData' (城市 {current_city_name}, 页 {current_page_num})"); return extracted_jobs, False, None, None

    has_more = zp_data.get("hasMore", False)
    page_level_lid_for_next_list = zp_data.get("lid")
    page_level_security_id_for_next_list = zp_data.get("securityId")
    job_list_from_json = zp_data.get("jobList")

    if job_list_from_json and isinstance(job_list_from_json, list):
        print(f"ℹ️ 第 {current_page_num} 页找到 {len(job_list_from_json)} 个原始职位条目，开始解析...")
        for job_item in job_list_from_json:
            # ... (内部解析逻辑与之前版本类似，确保调用 fetch_job_detail_api 时传递正确的参数) ...
            try:
                job_name = job_item.get("jobName", "N/A");
                salary_desc = job_item.get("salaryDesc", "N/A");
                brand_name = job_item.get("brandName", "N/A")
                location_name = job_item.get("locationName") or job_item.get("areaDistrict") or job_item.get(
                    "businessDistrict") or current_city_name
                display_location = f"{current_city_name} - {location_name}" if location_name != current_city_name else current_city_name
                experience = job_item.get("jobExperience", "N/A");
                education = job_item.get("jobDegree", "N/A");
                skills = job_item.get("skills", [])
                requirements_summary = " | ".join(filter(None, [experience, education] + skills))
                encrypt_job_id = job_item.get("encryptJobId");
                detail_api_security_id = job_item.get("securityId");
                detail_api_lid = job_item.get("lid")
                web_job_link = f"{BASE_URL}/job_detail/{encrypt_job_id}" if encrypt_job_id else "N/A"

                is_target_experience = False
                if EXPERIENCE_CODE is None or EXPERIENCE_CODE == '':
                    is_target_experience = True
                elif EXPERIENCE_CODE == '108':
                    if "实习" in job_name or any(
                        "实习" in str(req).lower() for req in [experience, education] + skills) or (
                            "经验不限" in experience and any(
                        term in job_name.lower() for term in ["实习", "intern"])): is_target_experience = True
                else:
                    if EXPERIENCE_CODE in requirements_summary: is_target_experience = True

                if is_target_experience:
                    job_data = {'搜索关键词': current_query, '城市': current_city_name, '职位名称': job_name,
                                '直达链接': web_job_link, '薪资待遇': salary_desc, '公司名称': brand_name,
                                '具体地点': display_location, '要求标签': requirements_summary, '职位描述': "待获取"}
                    if detail_api_security_id and detail_api_lid:
                        description = fetch_job_detail_api(session, detail_api_security_id, detail_api_lid,
                                                           current_query, current_city_code)
                        job_data['职位描述'] = description
                    else:
                        job_data['职位描述'] = "缺少参数无法调用详情API"
                    extracted_jobs.append(job_data)
            except Exception as e:
                print(f"❌ 解析单个JSON职位项时出错: {e}"); continue
        return extracted_jobs, has_more, page_level_lid_for_next_list, page_level_security_id_for_next_list
    else:
        print(f"ℹ️ API响应的 zpData 中无 'jobList' (城市 {current_city_name}, 页 {current_page_num})");
        return extracted_jobs, False, page_level_lid_for_next_list, page_level_security_id_for_next_list


def main():
    session = requests.Session()
    if not load_cookies_from_file(session, COOKIE_FILE):
        print("首次加载Cookie失败，脚本无法启动。请确保cookies.txt存在且有效。")
        return

    # call_set_token_api(session) # 初始setToken可选，可以先不调用

    all_jobs_across_all_searches = []
    keep_running = True  # 控制主循环是否继续

    for search_query_keyword in SEARCH_KEYWORDS_LIST:
        if not keep_running: break  # 如果用户选择退出，则跳出外层循环
        print(f"\n\n🔍 --- 开始爬取关键词: '{search_query_keyword}' ---")
        if all_jobs_across_all_searches:
            delay = random.uniform(INTER_KEYWORD_DELAY_MIN, INTER_KEYWORD_DELAY_MAX)
            print(f"  🔄 切换关键词，延时 {delay:.2f} 秒...")
            time.sleep(delay)

        for city_name_display, city_code_to_fetch in TARGET_CITIES.items():
            if not keep_running: break
            print(f"\n  🏙️ --- 开始爬取城市: {city_name_display} (代码: {city_code_to_fetch}) ---")

            current_search_jobs_list = []
            lid_for_next_list_page = None;
            security_id_for_next_list_page = None

            for page in range(1, MAX_PAGES_TO_SCRAPE_PER_CITY_KEYWORD + 1):
                if not keep_running: break
                print(f"\n  --- 正在处理 '{search_query_keyword}' - {city_name_display} - 第 {page} 页 ---")

                # 调用 fetch_job_list_page_html，它内部包含Cookie更新和重试逻辑
                job_page_api_response = fetch_job_list_page_html(
                    session, page, city_code_to_fetch, search_query_keyword, EXPERIENCE_CODE,
                    current_lid=lid_for_next_list_page, current_security_id=security_id_for_next_list_page)

                if not job_page_api_response:  # 如果彻底失败 (例如用户选择不更新Cookie并退出)
                    print(f"  ❌ 第 {page} 页数据获取彻底失败，可能需要手动干预或Cookie已失效。")
                    keep_running = False  # 设置标志停止后续所有操作
                    break
                    # 打印第一页列表API的响应摘要
                if page == 1:
                    print(f"  --- 第1页列表API响应码: {job_page_api_response.get('code')} ---")
                    if job_page_api_response.get("code") != 0:
                        print(json.dumps(job_page_api_response, indent=2, ensure_ascii=False))
                    elif job_page_api_response.get("zpData"):
                        pass
                    print("  --- JSON响应片段结束 ---\n")

                # 只有当列表API请求最终成功 (code == 0) 时才去解析
                if job_page_api_response.get("code") == 0:
                    parsed_jobs, has_more, next_lid_from_parse, next_security_id_from_parse = parse_job_data_from_api_html(
                        session, job_page_api_response, page, city_name_display, city_code_to_fetch,
                        search_query_keyword)

                    if not parsed_jobs and not has_more and job_page_api_response.get("zpData", {}).get(
                            "jobList") is not None and not job_page_api_response.get("zpData", {}).get("jobList"):
                        # API成功返回，zpData里有jobList但为空，且hasMore为false
                        print(f"  ℹ️ 第 {page} 页API返回空职位列表且无更多页面。")
                    elif parsed_jobs:
                        current_search_jobs_list.extend(parsed_jobs)
                        print(f"  👍 第 {page} 页成功解析 {len(parsed_jobs)} 个目标职位。")
                    else:
                        print(f"  ℹ️ 第 {page} 页未解析到目标职位。")

                    lid_for_next_list_page = next_lid_from_parse
                    security_id_for_next_list_page = next_security_id_from_parse

                    if not has_more: print(f"  ℹ️ API提示第 {page} 页后没有更多。"); break
                else:  # 如果列表API最终返回错误
                    print(f"  ❌ 列表API最终返回错误 (Code: {job_page_api_response.get('code')})，终止此关键词/城市。")
                    if job_page_api_response.get("code") in COOKIE_REFRESH_ERROR_CODES:
                        print("    这通常意味着Cookie已失效或需要验证。")
                    break

                if page < MAX_PAGES_TO_SCRAPE_PER_CITY_KEYWORD and has_more:
                    list_delay = random.uniform(LIST_PAGE_DELAY_MIN, LIST_PAGE_DELAY_MAX)
                    print(f"  📄 延时 {list_delay:.2f} 秒后获取下一列表页...")
                    time.sleep(list_delay)

            all_jobs_across_all_searches.extend(current_search_jobs_list)
            print(
                f"  ✅ --- 关键词 '{search_query_keyword}', 城市: {city_name_display} 处理完成, 共找到 {len(current_search_jobs_list)} 个职位 ---")
            if not keep_running: break
            # time.sleep(random.uniform(INTER_KEYWORD_DELAY_MIN / 2, INTER_KEYWORD_DELAY_MAX / 2))

    print(f"\n🎉🎉🎉 --- 所有爬取完成或用户选择退出 --- 🎉🎉🎉")
    # ... (后续的Excel/JSON输出逻辑不变) ...
    if all_jobs_across_all_searches:
        print(f"总共找到 {len(all_jobs_across_all_searches)} 个相关职位。\n")
        df = pd.DataFrame(all_jobs_across_all_searches)
        excel_columns_order = ['搜索关键词', '城市', '职位名称', '公司名称', '薪资待遇', '具体地点', '要求标签',
                               '职位描述', '直达链接']
        df_columns = [col for col in excel_columns_order if col in df.columns]
        df_to_save = df[df_columns]
        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        excel_output_filename = f"BOSS直聘_岗位_{'_'.join(TARGET_CITIES.keys())}_{timestamp_str}.xlsx"
        try:
            df_to_save.to_excel(excel_output_filename, index=False, engine='openpyxl')
            print(f"💾 结果已成功保存至 Excel 文件: '{excel_output_filename}'")
        except Exception as e:
            print(f"❌ 保存到 Excel 文件失败: {e}")
            json_output_filename = f"BOSS直聘_岗位_{'_'.join(TARGET_CITIES.keys())}_{timestamp_str}.json"
            with open(json_output_filename, 'w', encoding='utf-8') as f_out:
                json.dump(all_jobs_across_all_searches, f_out, ensure_ascii=False, indent=4)
            print(f"💾 结果已保存至 JSON 文件: '{json_output_filename}'")
        print("\n--- 控制台输出示例 (最多前2条) ---")
        for i, job in enumerate(all_jobs_across_all_searches[:2]):
            print(f"\n💼 --- 职位 {i + 1} ({job.get('搜索关键词', '')} - {job.get('城市', '')}) ---")
            for k, v_job in job.items():
                if k == '职位描述' and isinstance(v_job, str):
                    print(f"  {k}: {v_job[:60].replace(chr(10), ' ')}...")
                else:
                    print(f"  {k}: {v_job}")
            print("-" * 40)
    else:
        print("❌ 所有城市和关键词组合均未能找到任何符合条件的职位，或者在过程中退出了。")

if __name__ == '__main__':
    print("脚本开始运行。当提示更新Cookie时，请确保 cookies.txt 文件是最新且有效的！")
    print("在浏览器中完成一次成功搜索并立即导出Cookie，可以提高成功率。")
    main()