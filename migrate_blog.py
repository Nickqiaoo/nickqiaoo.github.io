#!/usr/bin/env python3

import os
import re
import shutil
import requests
from pathlib import Path
from datetime import datetime
import yaml
from urllib.parse import urlparse

def download_image(url, save_path):
    """下载图片到指定路径"""
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        with open(save_path, 'wb') as f:
            shutil.copyfileobj(response.raw, f)
        return True
    except Exception as e:
        print(f"下载图片失败 {url}: {e}")
        return False

def extract_images_from_content(content):
    """从内容中提取图片链接"""
    # 匹配markdown格式的图片 ![alt](url)
    img_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    matches = re.findall(img_pattern, content)
    
    images = []
    for alt, url in matches:
        if url.startswith('http'):
            images.append({'alt': alt, 'url': url, 'markdown': f'![{alt}]({url})'})
    
    return images

def sanitize_filename(filename):
    """清理文件名，移除不安全字符"""
    # 移除或替换不安全字符
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    filename = filename.replace(' ', '-')
    return filename

def convert_date_format(date_str):
    """转换日期格式"""
    try:
        # 解析原格式: 2022-03-13 22:00:00
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        # 转换为新格式: 13 March 2022
        return dt.strftime("%-d %B %Y")
    except:
        return date_str

def process_blog_post(file_path, source_dir, target_dir):
    """处理单个博客文章"""
    print(f"处理文章: {file_path.name}")
    
    # 读取文章内容
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 分离frontmatter和正文
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            frontmatter = parts[1].strip()
            body = parts[2].strip()
        else:
            print(f"无效的frontmatter格式: {file_path.name}")
            return
    else:
        print(f"缺少frontmatter: {file_path.name}")
        return
    
    # 解析frontmatter
    try:
        front_data = yaml.safe_load(frontmatter)
    except Exception as e:
        print(f"解析frontmatter失败 {file_path.name}: {e}")
        return
    
    # 创建文章目录
    title = front_data.get('title', file_path.stem)
    folder_name = sanitize_filename(title)
    article_dir = target_dir / folder_name
    article_dir.mkdir(exist_ok=True)
    
    # 处理图片
    images = extract_images_from_content(body)
    updated_body = body
    
    for i, img in enumerate(images):
        # 确定图片文件名
        parsed_url = urlparse(img['url'])
        original_filename = os.path.basename(parsed_url.path)
        if not original_filename or '.' not in original_filename:
            # 如果无法从URL获取文件名，使用默认格式
            original_filename = f"image_{i+1}.png"
        
        # 清理文件名
        clean_filename = sanitize_filename(original_filename)
        image_path = article_dir / clean_filename
        
        # 下载图片
        if download_image(img['url'], image_path):
            # 更新markdown中的图片引用
            new_img_ref = f"![{img['alt']}](./{clean_filename})"
            updated_body = updated_body.replace(img['markdown'], new_img_ref)
            print(f"  下载图片: {clean_filename}")
        else:
            print(f"  图片下载失败，保持原链接: {img['url']}")
    
    # 转换frontmatter格式
    new_frontmatter = {
        'title': f'"{title}"',
        'description': f'"{front_data.get("title", title)}"',  # 使用title作为description
        'publishDate': f'"{convert_date_format(str(front_data.get("date", "")))}"',
        'tags': front_data.get('tags', [])
    }
    
    # 如果有categories，添加到tags中
    if 'categories' in front_data:
        categories = front_data['categories']
        if isinstance(categories, list):
            new_frontmatter['tags'].extend(categories)
        else:
            new_frontmatter['tags'].append(categories)
    
    # 去重tags
    new_frontmatter['tags'] = list(set(new_frontmatter['tags'])) if new_frontmatter['tags'] else []
    
    # 生成新的markdown文件
    new_content = "---\n"
    new_content += f"title: {new_frontmatter['title']}\n"
    new_content += f"description: {new_frontmatter['description']}\n"
    new_content += f"publishDate: {new_frontmatter['publishDate']}\n"
    if new_frontmatter['tags']:
        new_content += f"tags: {new_frontmatter['tags']}\n"
    new_content += "---\n\n"
    new_content += updated_body
    
    # 写入新文件
    output_file = article_dir / "index.md"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print(f"  完成: {folder_name}/index.md")

def main():
    """主函数"""
    source_dir = Path("/Volumes/data/project/blog/nickqiaoo.github.io-source/source/_posts")
    target_dir = Path("/Volumes/data/project/blog/src/content/post")
    
    if not source_dir.exists():
        print(f"源目录不存在: {source_dir}")
        return
    
    if not target_dir.exists():
        print(f"目标目录不存在: {target_dir}")
        return
    
    # 获取所有markdown文件
    md_files = list(source_dir.glob("*.md"))
    
    print(f"找到 {len(md_files)} 个博客文章")
    
    for file_path in md_files:
        try:
            process_blog_post(file_path, source_dir, target_dir)
        except Exception as e:
            print(f"处理文章失败 {file_path.name}: {e}")
    
    print("博客迁移完成！")

if __name__ == "__main__":
    main()