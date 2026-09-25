import pyautogui
import cv2
import numpy as np
from utils import get_map, load_binary_map
import time

def aggressive_player_detection():
    """激进的玩家检测 - 尝试找到任何小地图上的亮色像素"""
    print("🔍 激进玩家检测模式...")
    print("⏳ 5秒后开始截屏...\n")
    
    for i in range(5, 0, -1):
        print(f"   {i}...")
        time.sleep(1)
    
    image = get_map()
    height, width = image.shape[:2]
    
    print(f"\n✅ 小地图尺寸: {width}x{height}")
    cv2.imwrite('./debug_aggressive_original.png', image)
    print("✅ 已保存原始小地图到 debug_aggressive_original.png\n")
    
    marked_image = image.copy()
    
    # 找出所有非黑色像素
    print("🔍 分析所有非黑色像素...\n")
    
    all_colors = {}
    for y in range(height):
        for x in range(width):
            b, g, r = image[y, x]
            # 任何不全是黑色的像素
            if b > 20 or g > 20 or r > 20:
                rgb = (r, g, b)
                if rgb not in all_colors:
                    all_colors[rgb] = 0
                all_colors[rgb] += 1
    
    # 按频率排序
    sorted_colors = sorted(all_colors.items(), key=lambda x: x[1], reverse=True)
    
    print("📋 小地图上出现次数最多的 15 种颜色 (RGB):")
    print("-" * 60)
    print("频率\t\tRGB值\t\t\tHEX值\t\t出现位置")
    print("-" * 60)
    
    for i, (rgb, count) in enumerate(sorted_colors[:15]):
        r, g, b = rgb
        hex_color = f"{r:02x}{g:02x}{b:02x}".lower()
        
        # 找出这个颜色出现的位置
        positions = []
        for y in range(height):
            for x in range(width):
                b_pixel, g_pixel, r_pixel = image[y, x]
                if (r_pixel, g_pixel, b_pixel) == rgb:
                    positions.append((x, y))
                    if len(positions) <= 3:
                        cv2.circle(marked_image, (x, y), 3, (0, 255, 255), -1)
        
        pos_str = f"{positions[:2]}" if positions else "无"
        print(f"{count}\t\t({r:3d},{g:3d},{b:3d})\t\t{hex_color}\t\t{pos_str}")
    
    cv2.imwrite('./debug_aggressive_marked.png', marked_image)
    print("\n✅ 已保存标记后的小地图到 debug_aggressive_marked.png")
    print("   (黄色圆点标记了最常见颜色的出现位置)\n")
    
    # 建议修改 utils.py
    if sorted_colors:
        print("="*60)
        print("💡 建议修改 utils.py 第 142 行：\n")
        
        top_10_colors = sorted_colors[:10]
        color_list = ", ".join([f'"{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"' for rgb, _ in top_10_colors])
        
        print(f"将原来的：")
        print(f'for color in ["afaca4", "aaa8a2", "aba8a1", "cbd442", "d6e947", "c3d233"]:\n')
        
        print(f"改为：")
        print(f'for color in [{color_list}]:\n')
        
        print("="*60)

if __name__ == "__main__":
    aggressive_player_detection()
