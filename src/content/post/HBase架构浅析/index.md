---
title: "HBase架构浅析"
description: "HBase架构浅析"
publishDate: "1 March 2020"
tags: ['hbase', 'zookeeper', '分布式']
---

Hbase是Google`BigTable`的开源实现，它的本质就是一个多维的map:`map<rowkey,map<column family:qualifier,value>>`，概念不作过多介绍，对于一个存储系统，我只关注这几个点，数据读的过程是怎样的？写的过程是怎样的？存储格式是怎样的？

<!-- more -->

## HBase架构

![arch](https://d2h0cx97tjks2p.cloudfront.net/blogs/wp-content/uploads/sites/2/2018/05/HBase-Components.png)

HBase由HMaster,Zookeeper,Regionserver组成，系统建立在HDFS基础上，也就是说数据是通过HDFS存储的。每个Regionserver负责一部分rowkey的存储，HMaster和Regionserver都将自身通过建立临时节点注册在Zookeeper上，HMaster watch zk来监控Regionserver的状态，HMaster自身通过zk进行选主实现高可用，然后就是做一些DDL操作，分配region等。

## 读过程

HBase有一个存放元数据的`hbase:meta`表：
- Key: region start key, region id
- Values: RegionServer info

Zookeeper维护存着这张表的Regionserver信息，所以客户端读数据时首先从Zookeeper获得存着这张表的Regionserver，然后从这个Regionserver获取rowkey分布的元信息，最后从对应的Regionserver获得数据。

## 写过程

有了rowkey分布的信息，写数据直接向对应的Regionserver发请求，Regionserver首先会写`WAL`（write ahead log）持久化到磁盘，然后写到内存中跳表实现的`MemStore`，这时就返回写成功了。

## 存储格式

每一个column family都有一个独立的MemStore，HBase同样基于LSM-tree的思想，MemStore由一个可写的Segment，以及一个或多个不可写的Segments构成，不同于LevelDB的Immutable Memtable直接dump为level0，多个Immutable Segments可以在内存中进行合并，当达到一定阈值以后才将内存中的数据持久化成HDFS中的HFile文件，HFile具体的格式就不作探讨了，同时HFile是有多种归并策略进行选择的，感兴趣的可以看参考中的第二篇文章。

## 注意事项

理解了HBase的大致读写流程，在设计的时候就需要注意，比如rowkey的设计，选择随机的rowkey数据就会分布在不同的Regionserver上，有序的rowkey那相邻的数据会在同一台Regionserver上，比如不同的column family在不同的HFile中，读的时候指定需要的column family可以提高性能。

## 参考

> https://data-flair.training/blogs/hbase-architecture/
> nosqlnotes.com/technotes/hbase/flush-compaction/